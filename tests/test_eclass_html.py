"""실제 eClass 페이지 구조에 대한 선택자 검증.

tests/fixtures/*.html 은 실제 페이지에서 구조만 뽑아 익명화한 것입니다.
개인정보(학번, 이름, 이메일)는 들어 있지 않습니다.

여기서 검증하는 것:
  crawler.py / browser.py 가 쓰는 선택자가 실제 마크업과 맞는지.
브라우저 없이 정규식으로 보므로 완벽하지는 않지만, 선택자를 고치다가
오타를 내는 사고는 잡습니다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gymhall.eclass import browser as B
from gymhall.eclass import crawler as C
from gymhall.eclass.urls import parse_course_option

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def css_to_regex(selector: str) -> str:
    """아주 단순한 CSS -> 정규식 변환. 테스트용입니다.

    속성값의 따옴표 종류(' vs ")는 무시합니다. 실제 마크업이 어느 쪽을
    쓰는지와 무관하게 선택자가 맞는지만 보려는 것이기 때문입니다.
    """
    selector = selector.split(",")[0].strip()
    last = selector.split()[-1]
    m = re.match(r"^(\w+)?((?:[.#][\w-]+)*)((?:\[[^\]]+\])*)$", last)
    if not m:
        pytest.skip(f"변환할 수 없는 선택자: {selector}")
    tag = m.group(1) or r"\w+"
    classes = m.group(2) or ""
    attrs = m.group(3) or ""

    pattern = rf"<{tag}\b"
    for token in re.findall(r"[.#][\w-]+", classes):
        name = token[1:]
        key = "class" if token[0] == "." else "id"
        pattern += rf'[^>]*{key}=["\'][^"\']*\b{re.escape(name)}\b'

    for attr in re.findall(r"\[([^\]]+)\]", attrs):
        if "*=" in attr:
            key, _, value = attr.partition("*=")
            value = value.strip("\"'")
            pattern += rf'[^>]*{re.escape(key)}=["\'][^"\']*{re.escape(value)}'
        elif "=" in attr:
            key, _, value = attr.partition("=")
            value = value.strip("\"'")
            pattern += rf'[^>]*{re.escape(key)}=["\']{re.escape(value)}["\']'
        else:
            pattern += rf"[^>]*{re.escape(attr)}"
    return pattern


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------
def test_id_and_password_selectors_match_logged_out_page():
    html = load("main_logged_out.html")
    assert any(re.search(css_to_regex(s), html) for s in B.ID_SELECTORS), B.ID_SELECTORS
    assert any(re.search(css_to_regex(s), html) for s in B.PW_SELECTORS), B.PW_SELECTORS


def test_submit_selector_matches_fnlogin():
    """로그인 버튼은 onclick='fnLogin()' 입니다."""
    html = load("main_logged_out.html")
    assert "fnLogin" in html
    assert any("fnLogin" in s for s in B.SUBMIT_SELECTORS)


def test_logged_in_markers_present_when_logged_in():
    html = load("main_logged_in.html")
    assert re.search(r"cmd=logoutUser", html)
    assert re.search(r"name=[\"']courseDTO\.courseId", html)


def test_logged_out_page_has_no_logged_in_markers():
    """로그인 전 화면을 '로그인됨' 으로 오판하면 비밀번호를 안 넣습니다."""
    html = load("main_logged_out.html")
    assert "cmd=logoutUser" not in html
    assert "courseDTO.courseId" not in html


def test_password_field_is_the_logged_out_signal():
    assert re.search(css_to_regex(B.LOGGED_OUT_SELECTOR), load("main_logged_out.html"))
    assert not re.search(css_to_regex(B.LOGGED_OUT_SELECTOR), load("main_logged_in.html"))


# ---------------------------------------------------------------------------
# 과목 목록
# ---------------------------------------------------------------------------
def test_course_options_parse():
    html = load("main_logged_in.html")
    options = re.findall(r"<option value='([^']*)'[^>]*>([^<]*)</option>", html)
    parsed = [parse_course_option(v) for v, _ in options]
    good = [p for p in parsed if p]
    assert len(good) == 4, parsed
    assert good[0][0] == "2026211605005"
    assert good[-1][0].startswith("CORS_")
    # '강의실 선택' 은 걸러져야 합니다
    assert parsed[0] is None


# ---------------------------------------------------------------------------
# 게시판
# ---------------------------------------------------------------------------
def test_board_table_selector_matches():
    html = load("board_list.html")
    assert re.search(css_to_regex(C.BOARD_TABLE), html), C.BOARD_TABLE


def test_board_title_link_selector_matches():
    html = load("board_list.html")
    assert any(re.search(css_to_regex(s), html)
               for s in C.BOARD_TITLE_LINK.split(",")), C.BOARD_TITLE_LINK


def test_attachment_indicator_present():
    """첨부 있는 글에만 icon-data-color 가 붙습니다."""
    html = load("board_list.html")
    assert html.count("icon-data-color") == 1


def test_board_rows_have_expected_column_order():
    """read_board_rows 의 JS 가 뒤에서 3번째를 작성자, 2번째를 작성일로
    읽습니다. 컬럼 순서가 바뀌면 메타데이터가 뒤섞입니다."""
    html = load("board_list.html")
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
    assert len(rows) == 2
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        assert len(cells) == 6, f"컬럼 수가 6이 아닙니다: {len(cells)}"
        text = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        assert text[0].isdigit(), text          # 번호
        assert text[-3] == "홍길동", text         # 작성자
        assert re.match(r"\d{4}-\d{2}-\d{2}", text[-2]), text  # 작성일
        assert text[-1].isdigit(), text         # 조회수


def test_download_iframe_exists():
    """첨부는 숨은 iframe 으로 내려옵니다. Playwright 의 download 이벤트가
    이걸 잡기 때문에 requests 로는 재현하기 어렵습니다."""
    assert "파일다운로드프레임" in load("board_list.html")


def test_navigation_is_form_post_not_links():
    """제목 링크가 href 로 이동한다면 Playwright 가 필요 없습니다.
    실제로는 javascript:void(0) 이고 숨은 form 을 POST 합니다."""
    html = load("board_list.html")
    titles = re.findall(r'<a class="clip boardTitleNcontent TITLE_ORIGIN" href="([^"]*)"', html)
    assert titles, "제목 링크를 찾지 못했습니다"
    assert all(t == "javascript:void(0)" for t in titles), titles


def test_fixtures_contain_no_personal_data():
    """픽스처에 실제 학번/이름/이메일이 섞여 들어가지 않게 합니다."""
    # @media 같은 CSS 를 이메일로 오해하지 않도록 주소 모양만 봅니다.
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]*\w")
    for path in FIXTURES.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not email.search(text), f"{path.name} 에 이메일: {email.search(text).group(0)}"
        # 0000000 은 일부러 넣은 자리표시자입니다
        real_ids = [m for m in re.findall(r"\b\d{7}\b", text) if m != "0000000"]
        assert not real_ids, f"{path.name} 에 학번 같은 7자리 숫자: {real_ids}"
        # 실명이 다시 섞여 들어가는 것을 막습니다(가명만 허용).
        assert "최성빈" not in text, f"{path.name} 에 실명"


# ---------------------------------------------------------------------------
# 실제 로그인 폼의 세부 사항
# ---------------------------------------------------------------------------
def test_login_form_posts_to_user_do():
    """폼은 /User.do 로 POST 하고 cmd=loginUser 를 숨은 필드로 보냅니다."""
    html = load("main_logged_out.html")
    assert re.search(r'<form[^>]*id="loginForm"[^>]*action="/User\.do"', html)
    assert re.search(r'<input[^>]*name="cmd"[^>]*value="loginUser"', html)


def test_native_form_submit_is_blocked():
    """onsubmit="return false" 라서 폼의 기본 제출은 막혀 있습니다.

    즉 Playwright 로 form.submit() 을 기대하면 안 되고, 버튼을 클릭하거나
    비밀번호 칸에서 Enter 를 눌러 fnLogin() 이 돌게 해야 합니다.
    """
    html = load("main_logged_out.html")
    assert 'onsubmit="return false;"' in html


def test_enter_key_triggers_login():
    """비밀번호 칸의 onkeypress 가 Enter 를 fnLogin() 으로 연결합니다.
    버튼 선택자가 전부 실패해도 Enter 폴백이 살아 있는 근거입니다."""
    html = load("main_logged_out.html")
    assert re.search(r'id="pw"[^>]*onkeypress="[^"]*keyCode == 13[^"]*fnLogin', html)


def test_id_attributes_differ_from_name_attributes():
    """회귀 방지: id 는 `id`/`pw` 이고 name 은 `userId`/`password` 입니다.
    둘을 같다고 가정하면(#userId, #password) 선택자가 전부 빗나갑니다."""
    html = load("main_logged_out.html")
    assert re.search(r'<input[^>]*id="id"[^>]*name="userId"', html)
    assert re.search(r'<input[^>]*name="password"[^>]*id="pw"', html)
    assert 'id="userId"' not in html
    assert 'id="password"' not in html


def test_our_id_selectors_do_not_rely_on_wrong_ids():
    """browser.py 가 존재하지 않는 #userId / #password 를 쓰지 않는지."""
    assert not any("#userId" in s for s in B.ID_SELECTORS), B.ID_SELECTORS
    assert not any("#password" in s and "name=" not in s for s in B.PW_SELECTORS), B.PW_SELECTORS


def test_submit_selector_matches_login_button():
    html = load("main_logged_out.html")
    assert any(re.search(css_to_regex(s), html) for s in B.SUBMIT_SELECTORS), B.SUBMIT_SELECTORS
    assert re.search(r'<a[^>]*onclick="fnLogin\(\);"[^>]*class="loginBtn"', html)
