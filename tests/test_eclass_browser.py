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
