"""과목 = 레포지터리.

한 PC 가 여러 과목을 담당할 수 있고, 과목마다 올라가는 레포가 다릅니다.
"어느 레포로 보낼지" 를 묻는 곳이 여러 군데라, 그 답은 **여기 한 곳**
에서만 냅니다. 라우팅이 두 곳에 있으면 한쪽만 고쳐져서 운영체제 녹화가
자료구조 레포로 갑니다.

설정 모양:

    [[eclass.course_repos]]
    id   = "2026211043701"
    name = "자료구조"
    repo = "datastructure-2026-2"
    # owner = "다른조직"        # 생략하면 github.owner

과목이 하나도 없으면(courses = []) 예전처럼 github.owner/repo 한 곳으로
올립니다. 과목을 적기 시작하면 그 목록이 이 PC 의 담당 전부입니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time as _time
from typing import Any


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    owner: str
    repo: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


class CourseError(ValueError):
    """과목 설정이 잘못됐습니다."""


_REPO_OK = re.compile(r"^[A-Za-z0-9._-]+$")


def _norm(value: Any) -> str:
    return str(value or "").strip()


def load(cfg) -> list[Course]:
    """설정에서 이 PC 가 담당하는 과목을 읽습니다.

    빠진 값은 조용히 넘기지 않습니다. 과목 하나가 레포 없이 들어가면
    그 과목만 엉뚱한 곳으로 가거나 통째로 빠지는데, 둘 다 실행 로그만
    봐서는 알아보기 어렵습니다.
    """
    # 주의: eclass.courses 는 '무엇을 수집할지' 고르는 필터이고,
    # 여기 eclass.course_repos 는 '어디로 올릴지' 입니다. 이름이 비슷해
    # 한 키로 합치고 싶어지는데, 합치면 "수집은 하되 올리지 않는 과목"
    # 을 표현할 수 없습니다.
    raw = cfg.get("eclass.course_repos", []) or []
    if isinstance(raw, dict):            # 실수로 [eclass.courses] 로 적은 경우
        raise CourseError(
            "eclass.course_repos 는 [[eclass.course_repos]] (두 겹 대괄호) 로 적어야 "
            "목록이 됩니다. 지금은 표 하나로 읽혔습니다."
        )
    default_owner = _norm(cfg.get("github.owner"))

    out: list[Course] = []
    seen_ids: dict[str, str] = {}
    seen_repos: dict[str, str] = {}
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise CourseError(f"eclass.course_repos[{i}] 가 표가 아닙니다: {item!r}")
        cid, name = _norm(item.get("id")), _norm(item.get("name"))
        repo = _norm(item.get("repo"))
        owner = _norm(item.get("owner")) or default_owner
        where = f"eclass.course_repos[{i}]" + (f" ({name})" if name else "")
        if not name:
            raise CourseError(f"{where}: name 이 비었습니다.")
        if not repo:
            raise CourseError(f"{where}: repo 가 비었습니다. 과목마다 레포가 하나입니다.")
        if not owner:
            raise CourseError(
                f"{where}: owner 를 정할 수 없습니다. github.owner 를 적거나 "
                "이 과목에 owner 를 직접 적으세요."
            )
        if not _REPO_OK.match(repo):
            raise CourseError(f"{where}: repo 이름에 쓸 수 없는 글자가 있습니다: {repo!r}")
        if cid and cid in seen_ids:
            raise CourseError(f"{where}: 과목 코드 {cid} 가 {seen_ids[cid]} 와 겹칩니다.")
        slug = f"{owner}/{repo}".lower()
        if slug in seen_repos:
            raise CourseError(
                f"{where}: 레포 {owner}/{repo} 가 {seen_repos[slug]} 와 겹칩니다. "
                "과목 = 레포지터리라면 두 과목이 같은 레포를 쓰면 안 됩니다."
            )
        if cid:
            seen_ids[cid] = where
        seen_repos[slug] = where
        out.append(Course(id=cid, name=name, owner=owner, repo=repo))
    return out


def by_id(courses: list[Course], course_id: str) -> Course | None:
    cid = _norm(course_id)
    return next((c for c in courses if cid and c.id == cid), None)


def by_name(courses: list[Course], name: str) -> Course | None:
    """과목명으로 찾습니다. '자료구조_01' 처럼 분반이 붙어도 맞춥니다."""
    want = _norm(name)
    if not want:
        return None
    for c in courses:                     # 완전히 같은 것 우선
        if c.name == want:
            return c
    low = want.lower()
    for c in courses:
        n = c.name.lower()
        if n and (low.startswith(n) or n in low):
            return c
    return None


def from_timetable(cfg, courses: list[Course], when) -> Course | None:
    """시각으로 시간표를 되짚어 과목을 찾습니다.

    Zoom 감지처럼 과목을 모르는 트리거가 쓸 마지막 수단입니다.
    시간표 밖이면 None — 그때는 업로드하지 않고 로컬에만 둡니다.
    '아무 과목이나 첫 번째로' 같은 폴백은 두지 않습니다. 운영체제
    녹화가 자료구조 레포로 가는 것이 안 올라가는 것보다 나쁩니다.
    """
    days = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
    today = days.get(when.weekday())
    now = when.time()
    for row in cfg.get("recording.timetable", []) or []:
        if not isinstance(row, dict) or _norm(row.get("day")).lower() != today:
            continue
        try:
            start = _time.fromisoformat(_norm(row.get("start")))
            end = _time.fromisoformat(_norm(row.get("end")))
        except ValueError:
            continue
        if not (start <= now <= end):
            continue
        hit = by_id(courses, row.get("course_id")) or by_name(courses, row.get("name"))
        if hit:
            return hit
    return None


def resolve(cfg, courses: list[Course], *, course_id: str = "", label: str = "",
            when=None) -> Course | None:
    """녹화 한 건이 어느 과목인지. 명시 > 이름 > 시간표 > None.

    None 은 '모름' 이고, 모르면 업로드하지 않습니다.
    """
    if not courses:
        return None
    return (
        by_id(courses, course_id)
        or by_name(courses, course_id)
        or by_name(courses, label)
        or (from_timetable(cfg, courses, when) if when is not None else None)
    )
