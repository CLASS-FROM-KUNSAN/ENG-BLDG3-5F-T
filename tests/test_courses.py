"""과목 = 레포지터리.

한 PC 가 여러 과목을 담당할 수 있고, 과목마다 레포가 다릅니다.
"어느 레포로 보낼지" 의 답은 courses 모듈 한 곳에서만 냅니다.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from gymhall import courses as C


class Cfg:
    """get(dotted) 만 흉내 내는 최소 설정."""

    def __init__(self, data):
        self._d = data

    def get(self, key, default=None):
        cur = self._d
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def cfg(courses=None, owner="kunsan-cs", timetable=None):
    return Cfg({
        "github": {"owner": owner},
        "eclass": {"course_repos": courses if courses is not None else []},
        "recording": {"timetable": timetable or []},
    })


TWO = [
    {"id": "2026211043701", "name": "자료구조", "repo": "datastructure-2026-2"},
    {"id": "2026211043802", "name": "운영체제", "repo": "os-2026-2"},
]


# --- 읽기와 검증 ------------------------------------------------------
def test_no_courses_means_the_single_repo_mode():
    assert C.load(cfg([])) == []


def test_two_courses_get_their_own_repos():
    got = C.load(cfg(TWO))
    assert [c.slug for c in got] == ["kunsan-cs/datastructure-2026-2", "kunsan-cs/os-2026-2"]


def test_a_course_can_override_the_owner():
    got = C.load(cfg([{"name": "자료구조", "repo": "ds", "owner": "other-org"}]))
    assert got[0].slug == "other-org/ds"


@pytest.mark.parametrize("bad,why", [
    ({"name": "", "repo": "ds"}, "name"),
    ({"name": "자료구조", "repo": ""}, "repo"),
    ({"name": "자료구조", "repo": "ds/슬래시"}, "repo"),
])
def test_a_broken_course_is_refused_not_skipped(bad, why):
    """빠진 값을 조용히 넘기면 그 과목만 엉뚱한 곳으로 가거나 빠집니다."""
    with pytest.raises(C.CourseError) as e:
        C.load(cfg([bad]))
    assert why in str(e.value)


def test_a_course_without_any_owner_is_refused():
    with pytest.raises(C.CourseError):
        C.load(cfg([{"name": "자료구조", "repo": "ds"}], owner=""))


def test_two_courses_cannot_share_one_repo():
    """과목 = 레포지터리라면 두 과목이 같은 레포를 쓰면 안 됩니다."""
    with pytest.raises(C.CourseError) as e:
        C.load(cfg([{"name": "A", "repo": "same"}, {"name": "B", "repo": "same"}]))
    assert "겹칩니다" in str(e.value)


def test_duplicate_course_ids_are_refused():
    with pytest.raises(C.CourseError):
        C.load(cfg([{"id": "1", "name": "A", "repo": "a"},
                    {"id": "1", "name": "B", "repo": "b"}]))


def test_single_bracket_table_is_caught_with_a_readable_message():
    """[[eclass.course_repos]] 를 [eclass.courses] 로 적는 실수가 흔합니다."""
    c = Cfg({"github": {"owner": "o"},
             "eclass": {"course_repos": {"name": "자료구조", "repo": "ds"}}})
    with pytest.raises(C.CourseError) as e:
        C.load(c)
    assert "두 겹" in str(e.value)


# --- 라우팅 -----------------------------------------------------------
def test_course_id_wins_over_everything():
    cs = C.load(cfg(TWO))
    got = C.resolve(cfg(TWO), cs, course_id="2026211043802", label="자료구조")
    assert got.name == "운영체제"


def test_a_label_with_a_section_suffix_still_matches():
    cs = C.load(cfg(TWO))
    assert C.resolve(cfg(TWO), cs, label="자료구조_01").name == "자료구조"


def test_an_unknown_label_resolves_to_nothing():
    """모르면 None 입니다. 모르는 채로 아무 과목에나 넣지 않습니다."""
    cs = C.load(cfg(TWO))
    assert C.resolve(cfg(TWO), cs, label="감지_Zoom_1234") is None


def test_it_never_falls_back_to_the_first_course():
    """운영체제 녹화가 자료구조 레포로 가는 것이 안 올라가는 것보다 나쁩니다."""
    cs = C.load(cfg(TWO))
    assert C.resolve(cfg(TWO), cs, label="") is None
    assert C.resolve(cfg(TWO), cs) is None


TIMETABLE = [
    {"name": "자료구조", "course_id": "2026211043701",
     "day": "mon", "start": "09:00", "end": "10:50"},
    {"name": "운영체제", "day": "mon", "start": "13:00", "end": "14:50"},
]


def test_the_timetable_resolves_a_detection_with_no_name():
    """Zoom 감지는 과목을 모릅니다. 시각으로 되짚습니다."""
    c = cfg(TWO, timetable=TIMETABLE)
    cs = C.load(c)
    when = datetime(2026, 11, 9, 9, 30)      # 월요일 09:30
    assert C.resolve(c, cs, label="감지_Zoom", when=when).name == "자료구조"


def test_a_timetable_row_without_course_id_matches_by_name():
    c = cfg(TWO, timetable=TIMETABLE)
    cs = C.load(c)
    when = datetime(2026, 11, 9, 13, 30)
    assert C.resolve(c, cs, label="감지_Zoom", when=when).name == "운영체제"


def test_outside_the_timetable_resolves_to_nothing():
    """보강·특강은 시간표 밖입니다. 로컬에만 두고 올리지 않습니다."""
    c = cfg(TWO, timetable=TIMETABLE)
    cs = C.load(c)
    when = datetime(2026, 11, 9, 22, 0)
    assert C.resolve(c, cs, label="감지_Zoom", when=when) is None


def test_another_weekday_does_not_match():
    c = cfg(TWO, timetable=TIMETABLE)
    cs = C.load(c)
    when = datetime(2026, 11, 10, 9, 30)     # 화요일
    assert C.resolve(c, cs, label="감지_Zoom", when=when) is None


def test_a_timetable_row_with_a_broken_time_is_skipped_not_fatal():
    c = cfg(TWO, timetable=[{"name": "자료구조", "day": "mon",
                             "start": "아홉시", "end": "10:50"}])
    cs = C.load(c)
    when = datetime(2026, 11, 9, 9, 30)
    assert C.resolve(c, cs, when=when) is None


# --- 업로더 라우팅 네 경우 --------------------------------------------
# 마지막이 중요합니다. 매핑을 적어 둔 PC 에서 과목을 모르는 녹화를
# 기본 레포로 흘리면 **A과목 녹화가 B과목 레포로 갑니다.**


class _Cfg(Cfg):
    """_uploader 가 쓰는 키까지 갖춘 설정."""

    def __init__(self, repos):
        super().__init__({
            "github": {"owner": "org", "repo": "fallback-repo",
                       "retry_count": 1, "retry_backoff": 0.1,
                       "split_size_mb": 1900, "draft": False, "prerelease": False},
            "eclass": {"course_repos": repos},
        })


REPOS = [{"id": "1", "name": "자료구조", "repo": "ds-2026-2"}]


def _uploader(cfg, course=None):
    from gymhall.cli import _uploader as f

    return f(cfg, dry_run=True, course=course)


def test_no_mapping_and_no_course_uses_the_default_repo():
    """예전 동작. 1과목 PC 와 기존 설정이 안 깨져야 합니다."""
    up = _uploader(_Cfg([]), None)
    assert up is not None and up.repo == "fallback-repo"


def test_no_mapping_but_a_course_still_uses_the_default_repo():
    up = _uploader(_Cfg([]), C.Course("1", "자료구조", "org", ""))
    assert up is not None and up.repo == "fallback-repo"


def test_a_mapped_course_goes_to_its_own_repo():
    cfg = _Cfg(REPOS)
    course = C.load(cfg)[0]
    up = _uploader(cfg, course)
    assert up is not None and up.repo == "ds-2026-2"


def test_an_unknown_course_is_skipped_once_mapping_exists():
    """여기서 기본 레포로 흘리면 A과목 녹화가 B과목 레포로 갑니다."""
    assert _uploader(_Cfg(REPOS), None) is None


def test_the_skip_is_logged_so_it_is_not_silent(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        _uploader(_Cfg(REPOS), None)
    assert "--course" in caplog.text
    assert "로컬에 그대로" in caplog.text
