"""실제 브라우저로 로그인 폼을 조작해 봅니다.

Playwright 와 Chromium 이 없으면 통째로 건너뜁니다(CI 기본).
정규식으로 보는 test_eclass_html.py 와 달리, 여기서는 진짜 DOM 에서
선택자가 잡히고 클릭/입력이 동작하는지를 확인합니다.

주의: evaluate() 에 '문장 문자열' 을 넘기지 마세요.
Playwright 가 그것을 두 번 평가해서, 부작용이 있는 코드는 두 번 실행됩니다.
반드시 () => {...} 형태로 넘기세요.
"""

from __future__ import annotations

import functools
import http.server
import os
import socket
import threading
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from gymhall.eclass import browser as B  # noqa: E402
from gymhall.eclass.urls import parse_course_option  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
CHROMIUM_CANDIDATES = [
    os.environ.get("CHROMIUM_PATH"),
    "/opt/pw-browsers/chromium",
]


@pytest.fixture(scope="module")
def server():
    """픽스처 HTML 을 로컬 HTTP 로 띄웁니다(file:// 는 제약이 많습니다)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(FIXTURES))

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", port), handler)
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None  # type: ignore[assignment]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        last = None
        for path in [None, *CHROMIUM_CANDIDATES]:
            if path is not None and not Path(path).exists():
                continue
            try:
                kwargs = {"headless": True, "args": ["--no-sandbox"]}
                if path:
                    kwargs["executable_path"] = path
                br = pw.chromium.launch(**kwargs)
                yield br
                br.close()
                return
            except Exception as exc:  # pragma: no cover
                last = exc
        pytest.skip(f"Chromium 을 띄울 수 없습니다: {last}")


@pytest.fixture
def page(browser, server):
    pg = browser.new_page()
    pg.goto(f"{server}/main_logged_out.html")
    yield pg
    pg.close()


def first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() > 0 and loc.is_visible():
            return loc, sel
    return None, None


# ---------------------------------------------------------------------------
def test_login_fields_are_found_in_a_real_dom(page):
    idl, idsel = first_visible(page, B.ID_SELECTORS)
    pwl, pwsel = first_visible(page, B.PW_SELECTORS)
    assert idl is not None, f"아이디 칸을 찾지 못했습니다: {B.ID_SELECTORS}"
    assert pwl is not None, f"비밀번호 칸을 찾지 못했습니다: {B.PW_SELECTORS}"
    # 첫 번째 후보가 맞아야 합니다(폴백에 의존하고 있다면 신호입니다)
    assert idsel == B.ID_SELECTORS[0]
    assert pwsel == B.PW_SELECTORS[0]


def test_fields_accept_input(page):
    idl, _ = first_visible(page, B.ID_SELECTORS)
    pwl, _ = first_visible(page, B.PW_SELECTORS)
    idl.fill("TESTID")
    pwl.fill("secret")
    assert page.input_value("#id") == "TESTID"
    assert page.input_value("#pw") == "secret"


def test_button_click_calls_fnlogin_exactly_once(page):
    """두 번 호출되면 로그인 시도가 두 번 가고, 계정 잠금 위험이 커집니다."""
    sub, sel = first_visible(page, B.SUBMIT_SELECTORS)
    assert sub is not None, f"로그인 버튼을 찾지 못했습니다: {B.SUBMIT_SELECTORS}"
    assert sel == B.SUBMIT_SELECTORS[0]
    page.evaluate("() => { window.__n = 0; window.fnLogin = () => { window.__n++; }; }")
    sub.click()
    assert page.evaluate("() => window.__n") == 1


def test_enter_in_password_calls_fnlogin(page):
    """버튼 선택자가 전부 빗나가도 Enter 폴백이 살아 있어야 합니다."""
    pwl, _ = first_visible(page, B.PW_SELECTORS)
    page.evaluate("() => { window.__n = 0; window.fnLogin = () => { window.__n++; }; }")
    pwl.press("Enter")
    assert page.evaluate("() => window.__n") == 1


def test_native_submit_is_blocked(page):
    """onsubmit='return false' 라서 requestSubmit 으로는 못 보냅니다.
    이게 Playwright 로 '클릭' 해야 하는 이유입니다."""
    page.evaluate("() => { window.__submitted = false; }")
    blocked = page.evaluate(
        """() => {
             const f = document.getElementById('loginForm');
             return f.getAttribute('onsubmit');
           }"""
    )
    assert blocked and "false" in blocked


def test_logged_out_detection(page):
    assert page.locator(B.LOGGED_OUT_SELECTOR).first.is_visible()
    assert not any(page.locator(s).count() for s in B.LOGGED_IN_SELECTORS)


def test_logged_in_detection(browser, server):
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/main_logged_in.html")
        found = [s for s in B.LOGGED_IN_SELECTORS if pg.locator(s).count() > 0]
        assert found, "로그인된 페이지를 로그인 전으로 오판합니다"
        assert pg.locator(B.LOGGED_OUT_SELECTOR).count() == 0
    finally:
        pg.close()


def test_course_options_are_scraped(browser, server):
    """crawler.list_courses() 가 쓰는 것과 같은 JS 로 과목을 긁습니다."""
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/main_logged_in.html")
        options = pg.evaluate(
            """() => Array.from(
                   document.querySelectorAll("select[name='courseDTO.courseId'] option")
               ).map(o => ({value: o.value, text: o.textContent.trim()}))"""
        )
        parsed = [parse_course_option(o["value"]) for o in options]
        good = [p for p in parsed if p]
        assert len(good) == 4, parsed
        assert parsed[0] is None, "'강의실 선택' 항목이 걸러지지 않았습니다"
        assert good[0][0] == "2026211605005"
    finally:
        pg.close()


def test_board_rows_are_scraped(browser, server):
    """crawler.read_board_rows() 와 같은 JS 를 실제 DOM 에 돌립니다."""
    from gymhall.eclass.crawler import BOARD_ROW

    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_list.html")
        rows = pg.evaluate(
            """(sel) => {
                 const rows = Array.from(document.querySelectorAll(sel));
                 return rows.map((tr, i) => {
                   const tds = Array.from(tr.querySelectorAll('td'));
                   if (!tds.length) return null;
                   const link = tr.querySelector('a');
                   const text = tds.map(td => (td.innerText||'').replace(/\\s+/g,' ').trim());
                   return {
                     index: i,
                     no: text[0] || '',
                     title: (link ? (link.innerText||'') : (text[1]||'')).replace(/\\s+/g,' ').trim(),
                     has_file: !!tr.querySelector('.icon-data-color'),
                     author: text.length > 3 ? text[text.length-3] : '',
                     date: text.length > 2 ? text[text.length-2] : '',
                   };
                 }).filter(Boolean).filter(r => r.title);
               }""",
            BOARD_ROW,
        )
        assert len(rows) == 2, rows
        first = rows[0]
        assert first["no"] == "2"
        assert first["title"] == "발표과제 및 조편성 관련 안내"
        assert first["author"] == "홍길동"
        assert first["date"] == "2026-10-28"
        assert first["has_file"] is True
        assert rows[1]["has_file"] is False, "첨부 없는 글을 있다고 봤습니다"
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# 게시판 페이지네이션
# ---------------------------------------------------------------------------
class _FakeBrowser:
    """Crawler 가 기대하는 최소 인터페이스만 제공합니다."""

    def __init__(self, page, timeout_ms=5000):
        self.page = page
        self.timeout_ms = timeout_ms

    def goto(self, url, wait="domcontentloaded"):
        self.page.goto(url, wait_until=wait)


def _crawler(page):
    import tempfile
    from pathlib import Path as _P

    from gymhall.eclass.crawler import Crawler
    from gymhall.eclass.urls import Site
    from gymhall.paths import Layout
    from gymhall.state import Store

    tmp = _P(tempfile.mkdtemp())
    layout = Layout(tmp)
    layout.ensure()
    return Crawler(
        _FakeBrowser(page), Site(), layout, Store.open(layout.state / "s.json"),
        term="2026-2학기", menus={}, risky={},
    )


def test_page_count_is_read(browser, server):
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_paged.html")
        assert _crawler(pg).board_page_count() == 3
    finally:
        pg.close()


def test_single_page_board_reports_one(browser, server):
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_list.html")
        assert _crawler(pg).board_page_count() == 1
    finally:
        pg.close()


def test_goto_page_changes_the_list(browser, server):
    """회귀 방지: 클릭은 됐는데 목록이 그대로인 경우를 성공으로 처리하면
    같은 페이지를 반복해서 긁게 됩니다."""
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_paged.html")
        c = _crawler(pg)
        first = c.read_board_rows()
        assert first[0]["no"] == "9"

        assert c.goto_board_page(2) is True
        second = c.read_board_rows()
        assert second[0]["no"] == "7", second
        assert second[0]["title"] == "7번 글 공지"

        assert c.goto_board_page(3) is True
        assert c.read_board_rows()[0]["no"] == "5"
    finally:
        pg.close()


def test_goto_nonexistent_page_fails_loudly(browser, server):
    """없는 페이지로 갔다고 거짓 보고하면 수집 누락을 모르게 됩니다."""
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_paged.html")
        assert _crawler(pg).goto_board_page(99) is False
    finally:
        pg.close()


def test_all_pages_are_collected(browser, server):
    """3페이지에 걸친 5건이 전부 읽히는지."""
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_paged.html")
        c = _crawler(pg)
        seen = []
        for n in range(1, c.board_page_count() + 1):
            if n > 1:
                assert c.goto_board_page(n)
            seen.extend(r["no"] for r in c.read_board_rows())
        assert seen == ["9", "8", "7", "6", "5"], seen
    finally:
        pg.close()


def test_attachment_flag_survives_pagination(browser, server):
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/board_paged.html")
        c = _crawler(pg)
        assert [r["has_file"] for r in c.read_board_rows()] == [True, False]
        c.goto_board_page(2)
        assert [r["has_file"] for r in c.read_board_rows()] == [False, True]
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# 대화상자 처리
# ---------------------------------------------------------------------------
def _attach_handler(page):
    """EclassBrowser 의 dialog 핸들러만 떼어 붙입니다."""
    collected: list[str] = []

    def on_dialog(dialog):
        msg = (dialog.message or "").strip()
        if msg:
            collected.append(msg)
        try:
            dialog.accept()
        except Exception:
            dialog.dismiss()

    page.on("dialog", on_dialog)
    return collected


def test_alert_text_is_captured_not_swallowed(browser, server):
    """회귀 방지: Playwright 는 기본적으로 alert 를 조용히 닫습니다.
    그러면 '비밀번호가 일치하지 않습니다' 같은 실패 사유를 영영 못 봅니다.
    """
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/main_logged_out.html")
        seen = _attach_handler(pg)
        pg.evaluate("() => { alert('비밀번호가 일치하지 않습니다.'); }")
        pg.wait_for_timeout(200)
        assert seen == ["비밀번호가 일치하지 않습니다."]
    finally:
        pg.close()


def test_confirm_is_accepted_so_page_proceeds(browser, server):
    """중복 로그인 안내처럼 확인을 눌러야 넘어가는 화면이 있습니다."""
    pg = browser.new_page()
    try:
        pg.goto(f"{server}/main_logged_out.html")
        _attach_handler(pg)
        result = pg.evaluate("() => confirm('중복 로그인입니다. 계속할까요?')")
        assert result is True, "confirm 을 거절하면 로그인이 진행되지 않습니다"
    finally:
        pg.close()


@pytest.mark.parametrize(
    "message,expect",
    [
        ("이미 로그인된 사용자입니다", "다른 곳에서 이미 로그인"),
        ("다른 곳에서 접속 중입니다", "다른 곳에서 이미 로그인"),
        ("비밀번호가 일치하지 않습니다", "아이디 또는 비밀번호"),
        ("아이디를 확인하세요", "아이디 또는 비밀번호"),
        ("비밀번호 5회 오류로 계정이 잠겼습니다", "계정이 잠겼"),
        ("알 수 없는 오류", ""),
    ],
)
def test_diagnose_maps_message_to_cause(message, expect):
    """오류 메시지가 '로그인 실패' 한 줄이면 사용자가 뭘 해야 할지 모릅니다."""
    from gymhall.eclass.browser import EclassBrowser
    from gymhall.eclass.urls import Site

    br = EclassBrowser.__new__(EclassBrowser)  # __init__ 없이 메서드만 씁니다
    br.site = Site()
    result = br.diagnose([message])
    if expect:
        assert expect in result, f"{message!r} -> {result!r}"
    else:
        assert result == ""


def test_lock_warning_takes_priority():
    """잠김은 중복/오타보다 먼저 알려야 합니다. 더 재시도하면 악화됩니다."""
    from gymhall.eclass.browser import EclassBrowser

    br = EclassBrowser.__new__(EclassBrowser)
    result = br.diagnose(["비밀번호 5회 오류로 계정이 잠겼습니다"])
    assert "잠겼" in result
    assert "재시도" in result
