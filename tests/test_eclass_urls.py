"""eClass URL 지도.

실제 페이지 덤프에서 뽑은 값과 일치하는지 고정합니다.
학교가 LMS 를 개편하면 이 테스트가 깨지는 게 아니라, 수집이 조용히
빈손으로 돌아옵니다. 그래서 여기서는 '우리가 뭘 가정하고 있는지' 를
명시적으로 기록해 두는 역할을 합니다.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

import pytest

from gymhall.eclass import urls as U

COURSE = "2026211043701"
SITE = U.Site("https://eclass.kunsan.ac.kr")


def q(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlparse(url).query))


def test_course_home():
    url = SITE.course_home(COURSE)
    assert urlparse(url).path == "/Course.do"
    params = q(url)
    assert params["cmd"] == "viewStudyHome"
    assert params["courseDTO.courseId"] == COURSE


def test_open_course_uses_lesson_entry():
    """openType == 'open' 인 공개강좌는 진입점이 다릅니다."""
    url = SITE.open_course_home(COURSE)
    assert urlparse(url).path == "/Lesson.do"
    assert q(url)["cmd"] == "viewLessonContentsList"


@pytest.mark.parametrize(
    "key,parent,menu",
    [
        ("curriculum", U.PARENT_CURRICULUM, "menu_00092"),
        ("lecture_list", U.PARENT_CURRICULUM, "menu_00099"),
        ("attendance", U.PARENT_ACTIVITY, "menu_00082"),
        ("assignment", U.PARENT_ACTIVITY, "menu_00063"),
        ("discussion", U.PARENT_ACTIVITY, "menu_00065"),
        ("exam", U.PARENT_ACTIVITY, "menu_00067"),
        ("survey", U.PARENT_ACTIVITY, "menu_00069"),
        ("team", U.PARENT_ACTIVITY, "menu_00071"),
    ],
)
def test_menu_ids_match_the_dump(key, parent, menu):
    url = SITE.menu_url(COURSE, key)
    params = q(url)
    assert params["cmd"] == "moveCourseMenu"
    assert params["mainDTO.parentMenuId"] == parent
    assert params["mainDTO.menuId"] == menu
    assert params["courseDTO.courseId"] == COURSE


@pytest.mark.parametrize(
    "kind,board_class,suffix",
    [("notice", "notice", "N"), ("qna", "qna", "Q"), ("materials", "pds", "P")],
)
def test_board_urls(kind, board_class, suffix):
    url = SITE.board(COURSE, kind)
    params = q(url)
    assert urlparse(url).path == "/Course.do"
    assert params["cmd"] == "viewBoardContentsList"
    assert params["boardInfoDTO.boardClass"] == board_class
    assert params["boardInfoDTO.boardInfoId"] == f"{COURSE}-{suffix}"
    assert params["mainDTO.menuId"] == U.MENU_BOARD


def test_board_rejects_unknown_kind():
    with pytest.raises(ValueError):
        SITE.board(COURSE, "nope")


def test_menu_id_override():
    """학교가 menuId 를 바꿨을 때 config 로 덮어쓸 수 있어야 합니다."""
    url = SITE.menu_url(COURSE, "assignment", {"assignment": "menu_99999"})
    assert q(url)["mainDTO.menuId"] == "menu_99999"


@pytest.mark.parametrize("key", ["syllabus", "course_info", "grade"])
def test_unmappable_menus_return_none(key):
    """URL 을 만들 수 없는 메뉴는 None 을 돌려줘서, crawler 가
    좌측 메뉴 링크를 클릭하는 경로로 넘어가게 합니다."""
    assert SITE.menu_url(COURSE, key) is None


def test_base_url_trailing_slash_is_handled():
    site = U.Site("https://eclass.kunsan.ac.kr/")
    assert "//Course.do" not in site.course_home(COURSE)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026211043701,심영제,normal", ("2026211043701", "심영제", "normal")),
        ("CORS_260422171741a6f75263,오상현,bnormal",
         ("CORS_260422171741a6f75263", "오상현", "bnormal")),
        (" 2026210176501 , 김진욱 , normal ", ("2026210176501", "김진욱", "normal")),
    ],
)
def test_parse_course_option(raw, expected):
    assert U.parse_course_option(raw) == expected


@pytest.mark.parametrize("raw", ["", "강의실 선택", ",,", "only,two"])
def test_parse_course_option_rejects_junk(raw):
    assert U.parse_course_option(raw) is None


def test_risky_menus_are_exactly_the_three():
    """시험/설문/토론이 위험 목록에서 빠지면 크롤러가 상세로 진입합니다."""
    assert {"exam", "survey", "discussion"} == U.RISKY_MENUS


def test_every_menu_key_has_a_label_and_link_text():
    """새 메뉴를 추가하고 라벨을 빼먹으면 폴더 이름이 내부 키가 됩니다."""
    keys = set(U.MENU_LABELS)
    assert keys == set(U.MENU_LINK_TEXTS), keys ^ set(U.MENU_LINK_TEXTS)


def test_board_kinds_are_menu_keys():
    assert set(U.BOARD_KINDS) <= set(U.MENU_LABELS)
