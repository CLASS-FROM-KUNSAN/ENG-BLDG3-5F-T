"""과목 셀렉터 퍼지 매칭."""

from syncnote.eclass import coursematch as cm

COURSES = [
    ("C001", "운동과건강"),
    ("C002", "영어2"),
    ("C003", "자료구조및알고리즘"),
    ("C004", "Python 프로그래밍"),
]


def _sel(selectors, **kw):
    return cm.select(COURSES, selectors, id_of=lambda c: c[0],
                     name_of=lambda c: c[1], **kw)


def test_spacing_and_case_do_not_matter():
    r = _sel(["운동 과 건강", "python프로그래밍"])
    assert [c[0] for c in r.selected] == ["C001", "C004"]


def test_syllable_order_does_not_matter():
    assert cm.ratio("건강과운동", "운동과건강") == 1.0
    assert _sel(["건강과운동"]).selected == [COURSES[0]]


def test_short_alias_matches_by_containment():
    assert _sel(["자료구조"]).selected == [COURSES[2]]


def test_course_code_exact_match_wins():
    r = _sel(["c003"])
    assert r.selected == [COURSES[2]] and r.matches[0].score == 1.0


def test_below_threshold_is_skipped_never_falls_back_to_all():
    r = _sel(["양자역학"])
    assert r.selected == [] and r.all_failed


def test_duplicates_are_removed():
    r = _sel(["운동과건강", "건강 운동과", "C001"])
    assert r.selected == [COURSES[0]]


def test_split_selectors():
    assert cm.split_selectors("영어2, 운동과건강;자료구조\n\n") == [
        "영어2", "운동과건강", "자료구조"]
    assert cm.split_selectors("") == []


# -- 크롤러 필터 연결 ------------------------------------------------------
def _filter(include, exclude=()):
    from types import SimpleNamespace

    from syncnote.eclass.crawler import Course, Crawler, Stats

    courses = [Course(cid, name) for cid, name in COURSES] + [Course("C005", "영어1")]
    fake = SimpleNamespace(stats=Stats())
    out = Crawler.filter_courses(fake, courses, list(include), list(exclude))
    return [c.course_id for c in out], fake.stats


def test_filter_substring_still_takes_every_hit():
    ids, _ = _filter(["영어"])
    assert ids == ["C002", "C005"]


def test_filter_falls_back_to_fuzzy_for_reordered_names():
    ids, stats = _filter(["건강 운동과", "영어2"])
    assert ids == ["C001", "C002"] and not stats.errors


ALL_IDS = ["C001", "C002", "C003", "C004", "C005"]


def test_filter_unmatched_selector_falls_back_to_all_courses():
    ids, stats = _filter(["양자역학"])
    assert ids == ALL_IDS and not stats.errors


def test_filter_partial_miss_also_takes_all_so_nothing_is_lost():
    ids, _ = _filter(["영어2", "양자역학"])
    assert ids == ALL_IDS


def test_filter_fallback_still_honours_exclude():
    ids, _ = _filter(["양자역학"], exclude=["영어"])
    assert ids == ["C001", "C003", "C004"]
