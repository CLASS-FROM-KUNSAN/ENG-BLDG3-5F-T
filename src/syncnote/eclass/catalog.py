"""eClass 과목 목록(과목코드 ↔ 이름)을 이 PC 에 남겨 두고 찾기.

과목코드는 eClass 에 로그인해야만 보입니다(크롤러가 강의실 선택 상자에서
읽음). 시간표 편집기는 네트워크 없이 돌아야 하므로, 수집할 때 본 목록을
`<data_dir>/state/eclass_courses.json` 에 남겨 두고 편집기가 그것을 읽습니다.

남기는 것: 과목코드, 과목명, 개설 유형, 학기, 본 시각.
남기지 않는 것: 교수 이름 — 사람 이름이라 필요 없는 곳에 두지 않습니다.

편집기가 쓰는 목록은 세 곳을 합칩니다(앞쪽이 우선).
  1. 위 파일(수집 때 본 목록, 코드 있음)
  2. config 의 [[eclass.course_repos]] (id 를 적었으면 코드 있음)
  3. 수집 폴더 eclass/<학기>/<과목명>/ (이름만)
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..paths import ECLASS_NON_COURSE_DIRS
from . import coursematch

FILE_NAME = "eclass_courses.json"


@dataclass(frozen=True)
class Entry:
    id: str          # 과목코드. 수집 폴더에서만 알게 된 과목은 ""
    name: str
    term: str = ""
    source: str = ""  # "eclass" | "config" | "folder"

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.id:
            bits.append(f"[{self.id}]")
        if self.term:
            bits.append(f"· {self.term}")
        return " ".join(bits)


# ---------------------------------------------------------------------------
# 저장 (크롤러가 부름)
# ---------------------------------------------------------------------------
def save(state_dir: Path, term: str, courses: Iterable) -> Path:
    """이번 학기 목록을 저장. 다른 학기 목록은 남깁니다.

    courses 는 course_id·name 속성을 가진 것(crawler.Course)이면 됩니다.
    open_type 이 있으면 같이 남깁니다. professor 는 일부러 버립니다.
    """
    path = Path(state_dir) / FILE_NAME
    data = _read_raw(path)
    rows = []
    seen: set[str] = set()
    for c in courses:
        cid = str(getattr(c, "course_id", "") or "").strip()
        name = str(getattr(c, "name", "") or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        row = {"id": cid, "name": name or cid}
        open_type = str(getattr(c, "open_type", "") or "").strip()
        if open_type and open_type != "normal":
            row["open_type"] = open_type
        rows.append(row)
    data.setdefault("terms", {})[str(term)] = {
        "seen_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "courses": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _read_raw(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {"version": 1, "terms": {}}
    if not isinstance(data, dict) or not isinstance(data.get("terms"), dict):
        return {"version": 1, "terms": {}}
    return data


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------
def load(state_dir: Path) -> list[Entry]:
    """저장된 목록. 최근 학기가 먼저."""
    data = _read_raw(Path(state_dir) / FILE_NAME)
    # 본 시각이 최근인 학기 먼저. 같은 초에 저장했으면 학기 이름이 큰 쪽(2026-2 > 2026-1).
    terms = sorted(data["terms"].items(),
                   key=lambda kv: (str(kv[1].get("seen_at", "")) if isinstance(kv[1], dict)
                                   else "", str(kv[0])),
                   reverse=True)
    out: list[Entry] = []
    for term, block in terms:
        for row in block.get("courses", []) if isinstance(block, dict) else []:
            if isinstance(row, dict) and str(row.get("id", "")).strip():
                out.append(Entry(str(row["id"]).strip(), str(row.get("name", "")).strip(),
                                 str(term), "eclass"))
    return out


def from_config(raw_course_repos) -> list[Entry]:
    out = []
    for item in raw_course_repos or []:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            out.append(Entry(str(item.get("id", "") or "").strip(),
                             str(item["name"]).strip(), "", "config"))
    return out


def from_folders(eclass_dir: Path) -> list[Entry]:
    """수집 폴더의 과목명(코드 없음). 최근 학기 폴더가 먼저."""
    root = Path(eclass_dir)
    if not root.is_dir():
        return []
    out = []
    for term_dir in sorted((d for d in root.iterdir() if d.is_dir()), reverse=True):
        for d in sorted(term_dir.iterdir()):
            if d.is_dir() and d.name not in ECLASS_NON_COURSE_DIRS:
                out.append(Entry("", d.name, term_dir.name, "folder"))
    return out


def merge(*sources: Iterable[Entry]) -> list[Entry]:
    """앞쪽 출처 우선으로 합침. 같은 코드는 하나, 코드 없는 것은 같은 이름이
    이미 있으면 버립니다(코드 있는 쪽을 남기려고)."""
    out: list[Entry] = []
    ids: set[str] = set()
    names: set[str] = set()
    for src in sources:
        for e in src:
            key = e.name.strip()
            if e.id:
                if e.id in ids:
                    continue
                ids.add(e.id)
            elif key in names:
                continue
            names.add(key)
            out.append(e)
    return out


def gather(data_dir: Path | None, raw_course_repos=()) -> list[Entry]:
    if data_dir is None:
        return merge(from_config(raw_course_repos))
    d = Path(data_dir)
    return merge(load(d / "state"), from_config(raw_course_repos), from_folders(d / "eclass"))


# ---------------------------------------------------------------------------
# 이름 -> 과목
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Suggestion:
    entry: Entry | None
    score: float
    ambiguous: bool = False

    @property
    def confident(self) -> bool:
        return self.entry is not None and not self.ambiguous

    def as_dict(self) -> dict:
        return {"entry": asdict(self.entry) if self.entry else None,
                "score": self.score, "ambiguous": self.ambiguous}


def suggest(name: str, entries: list[Entry], course_id: str = "") -> Suggestion:
    """시간표 수업 이름(또는 과목코드)에 가장 가까운 eClass 과목.

    띄어쓰기·음절 순서 무관 퍼지 매칭(coursematch)을 그대로 씁니다.
    임계값 미만이면 entry=None. 1·2위가 붙으면 ambiguous.
    """
    if not entries:
        return Suggestion(None, 0.0)
    if course_id.strip():
        hit = next((e for e in entries if e.id == course_id.strip()), None)
        if hit is not None:
            return Suggestion(hit, 1.0)
    if not name.strip():
        return Suggestion(None, 0.0)
    res = coursematch.select(entries, [name], id_of=lambda e: e.id or f"name:{e.name}",
                             name_of=lambda e: e.name)
    m = res.matches[0]
    return Suggestion(m.course, m.score, m.ambiguous)
