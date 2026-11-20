"""과목 셀렉터 → 실제 과목 퍼지 매칭.

GitHub Action 환경변수(`SYNCNOTE_ECLASS_COURSES`)나 CLI 인자로 '수집할 과목'을
자유 텍스트로 받습니다. 사용자가 **띄어쓰기를 어떻게 하든, 음절/철자 순서를
어떻게 바꿔 적든** 비슷한 과목을 찾아내야 하므로:

- 정규화: NFKC → casefold → 영숫자·한글만 남김 → NFD(한글 음절을 자모로 분해)
  → '문자 멀티셋(Counter)'. 공백·구두점을 버려 **띄어쓰기 무관**, 멀티셋이라
  순서를 버려 **철자/음절 순서 무관**(애너그램도 매칭).
- 일치율: 멀티셋 Dice 계수 `2·공통/(|a|+|b|)` ∈ [0,1]. 애너그램이면 1.0.
  짧은 약칭이 긴 정식명에 들어갈 때를 위해 containment(`공통/|질의|`)도 보고
  둘 중 큰 값을 씁니다.
- 선택: 셀렉터마다 **최고 일치율** 과목을 채택. 임계값 미만이면 '매칭 없음'으로
  건너뜁니다(절대 전체 수집으로 폴백하지 않음). 과목코드 정확 일치는 1.0 fast-path.

크롤러(`Crawler.filter_courses`)는 이름 일부로 걸리는 셀렉터를 예전처럼
먼저 처리하고, 하나도 안 걸린 셀렉터만 여기로 넘깁니다.

브라우저·네트워크가 필요 없는 순수 로직이라 단위 테스트가 쉽습니다.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# 기본 임계값: 이보다 낮으면 '확신 매칭 없음' 으로 본다.
DEFAULT_THRESHOLD = 0.55
# 1·2위 차이가 이보다 작으면 '모호' 로 보고 경고한다(선택은 상위).
DEFAULT_MARGIN = 0.05


def normalize(text: str) -> Counter[str]:
    """문자열을 '문자 멀티셋' 으로. 순서·공백·구두점을 버린다.

    NFKC 로 전각/호환문자를 통일하고, casefold 로 영문 대소문자를 없앤 뒤,
    영숫자·한글만 남기고, NFD 로 한글 음절을 자모로 분해한다. 반환은 Counter
    라서 같은 글자가 여러 번이면 그만큼 센다(멀티셋).
    """
    s = unicodedata.normalize("NFKC", text or "").casefold()
    kept = "".join(ch for ch in s if ch.isalnum())
    decomposed = unicodedata.normalize("NFD", kept)
    return Counter(decomposed)


def _dice(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    inter = sum((a & b).values())
    total = sum(a.values()) + sum(b.values())
    return (2.0 * inter / total) if total else 0.0


def _containment(query: Counter[str], cand: Counter[str]) -> float:
    """질의가 후보에 얼마나 담겨 있는가(약칭 보정용)."""
    qn = sum(query.values())
    if not qn or not cand:
        return 0.0
    return sum((query & cand).values()) / qn


def ratio(query: str, candidate: str) -> float:
    """두 문자열의 일치율 ∈ [0,1]. 순서/공백 무관."""
    q, c = normalize(query), normalize(candidate)
    return max(_dice(q, c), 0.9 * _containment(q, c))


@dataclass
class Match:
    """셀렉터 하나의 매칭 결과."""
    selector: str
    course: Any | None          # 채택된 과목(없으면 None)
    score: float                # 최고 일치율
    runner_up: float = 0.0      # 2위 일치율(모호성 판단용)
    ambiguous: bool = False     # 1·2위가 margin 이내로 붙음


@dataclass
class SelectResult:
    selected: list[Any] = field(default_factory=list)   # 중복 제거된 채택 과목
    matches: list[Match] = field(default_factory=list)  # 셀렉터별 결과
    report: list[str] = field(default_factory=list)     # 사람이 읽는 로그 줄

    @property
    def any_selected(self) -> bool:
        return bool(self.selected)

    @property
    def all_failed(self) -> bool:
        """셀렉터는 있었는데 하나도 못 뽑았는가(= CI 에서 전체 수집 사고 방지)."""
        return bool(self.matches) and not self.selected


def select(
    courses: Sequence[Any],
    selectors: Iterable[str],
    *,
    id_of: Callable[[Any], str],
    name_of: Callable[[Any], str],
    threshold: float = DEFAULT_THRESHOLD,
    margin: float = DEFAULT_MARGIN,
) -> SelectResult:
    """셀렉터들을 실제 과목에 매칭한다.

    각 셀렉터는 (1) 과목코드와 정확 일치하면 그 과목(1.0), 아니면 (2) 이름
    일치율이 가장 높은 과목을 채택한다. 최고점이 threshold 미만이면 그
    셀렉터는 버린다. 결과 과목은 course_id 로 중복 제거한다.
    """
    result = SelectResult()
    chosen_ids: set[str] = set()

    for raw in selectors:
        sel = (raw or "").strip()
        if not sel:
            continue

        # (1) 과목코드 정확 일치 — 가장 확실.
        nsel = normalize(sel)
        exact = next((c for c in courses if normalize(id_of(c)) == nsel and nsel), None)
        if exact is not None:
            m = Match(sel, exact, 1.0)
            result.matches.append(m)
            _append(result, chosen_ids, m, id_of)
            result.report.append(f'"{sel}" → {name_of(exact)} (과목코드 정확 일치)')
            continue

        # (2) 이름 일치율 최댓값.
        scored = sorted(
            ((ratio(sel, name_of(c)), id_of(c), c) for c in courses),
            key=lambda t: (-t[0], t[1]),   # 점수 내림차순, 동점은 course_id 오름차순(결정적)
        )
        if not scored:
            result.matches.append(Match(sel, None, 0.0))
            result.report.append(f'"{sel}" → 과목 목록이 비어 매칭 불가')
            continue

        best_score, _bid, best = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0.0
        ambiguous = len(scored) > 1 and (best_score - runner) < margin and best_score > 0

        if best_score >= threshold:
            m = Match(sel, best, best_score, runner, ambiguous)
            result.matches.append(m)
            _append(result, chosen_ids, m, id_of)
            line = f'"{sel}" → {name_of(best)} (일치율 {best_score:.0%})'
            if ambiguous:
                line += f" ⚠ 모호: 2위 {name_of(scored[1][2])} {runner:.0%} — 상위 채택"
            result.report.append(line)
        else:
            result.matches.append(Match(sel, None, best_score, runner, ambiguous))
            result.report.append(
                f'"{sel}" → 임계값({threshold:.0%}) 미달 (최고 {name_of(best)} '
                f"{best_score:.0%}) — 건너뜀"
            )

    return result


def _append(result: SelectResult, chosen_ids: set[str], m: Match,
            id_of: Callable[[Any], str]) -> None:
    if m.course is None:
        return
    cid = id_of(m.course)
    if cid not in chosen_ids:
        chosen_ids.add(cid)
        result.selected.append(m.course)


def split_selectors(raw: str) -> list[str]:
    """환경변수/인자 문자열을 셀렉터 목록으로. 콤마·줄바꿈·세미콜론 구분."""
    if not raw:
        return []
    out: list[str] = []
    for chunk in raw.replace(";", "\n").replace(",", "\n").splitlines():
        chunk = chunk.strip()
        if chunk:
            out.append(chunk)
    return out
