"""한 파일짜리 전체 HTML 캡처(SingleFile 방식) + 개인정보 마스킹.

렌더된 DOM 을 직렬화하고 이미지/CSS 를 data URI 로 인라인, 스크립트는 제거,
개인정보는 마스킹합니다. 과제/첨부 '파일 다운로드' 와는 별개 기능입니다.
"""

from __future__ import annotations

import base64
import http.server
import os
import socket
import threading
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from syncnote.eclass import singlefile  # noqa: E402

CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]

# 1x1 투명 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
CSS = b"body{color:#123456} .bg{background:url('/bg.png')}"
PAGE = """<!doctype html><html><head>
  <meta charset="utf-8">
  <link rel="stylesheet" href="/style.css">
</head><body>
  <div id="header"><li class="w230"><dl><dt class="fontB">홍길동</dt></dl></li></div>
  <h1>과목 정보</h1>
  <img id="pic" src="/img.png" alt="과목 사진" title="학번 2601057 참고">
  <p>학번 2601057 · 이메일 user@test.ac.kr · 전화 010-1234-5678</p>
  <p>작성자: 김교수(1234567)</p>
  <input type="hidden" name="secret" value="should-be-cleared">
  <input type="password" value="pw">
  <script>window.__ran = true; document.title = 'JS RAN';</script>
</body></html>"""


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/img.png") or self.path.startswith("/bg.png"):
                self._send(PNG, "image/png")
            elif self.path.startswith("/style.css"):
                self._send(CSS, "text/css")
            else:
                self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
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
                kw = {"headless": True, "args": ["--no-sandbox"]}
                if path:
                    kw["executable_path"] = path
                br = pw.chromium.launch(**kw)
                yield br
                br.close()
                return
            except Exception as exc:  # pragma: no cover
                last = exc
        pytest.skip(f"Chromium 을 띄울 수 없습니다: {last}")


@pytest.fixture
def captured(browser, server):
    pg = browser.new_page()
    pg.goto(f"{server}/page.html", wait_until="domcontentloaded")
    html = singlefile.capture(pg, own_names=("홍길동",))
    yield html, pg
    pg.close()


def test_images_are_inlined_as_data_uri(captured):
    html, _ = captured
    assert "data:image/png;base64," in html, "이미지가 data URI 로 인라인되지 않았습니다"
    assert 'src="/img.png"' not in html


def test_stylesheet_is_inlined(captured):
    html, _ = captured
    assert "<style" in html and "#123456" in html, "외부 CSS 가 인라인되지 않았습니다"
    assert '<link rel="stylesheet"' not in html
    # CSS url() 배경 이미지도 data URI 로
    assert "url('/bg.png')" not in html


def test_scripts_are_removed(captured):
    html, pg = captured
    assert "<script" not in html.lower(), "스크립트가 남아있습니다"
    # 캡처 과정에서 원본 스크립트가 실행돼 title 을 바꾸지 않았는지(제거는 DOM만)
    assert "window.__ran" not in html


def test_known_names_mask_bare_third_party_names(browser):
    """사번 없는 제3자 '맨 이름' 도 레지스트리(own_names)에 있으면 전부 가립니다.

    과거엔 첫 이름 하나만 가렸습니다 — 수집하며 알게 된 모든 이름을 넘기면
    자유 본문의 그 이름들도 가려야 합니다. 모르는 이름은 그대로 둡니다."""
    pg = browser.new_page()
    try:
        pg.set_content(
            "<!doctype html><meta charset=utf-8><body>"
            "<p>작성자 김철수. 담당 이영희 선생님. 문의는 박민수에게.</p></body>"
        )
        html = singlefile.capture(pg, own_names=("김철수", "이영희"))
    finally:
        pg.close()
    assert "김철수" not in html and "이영희" not in html
    assert "[이름]" in html
    assert "박민수" in html, "레지스트리에 없는 이름까지 함부로 가리면 안 됩니다"


def test_pii_is_masked(captured):
    html, _ = captured
    # 플레이스홀더는 각괄호가 아니라 대괄호([ ]) — HTML 에서 안 숨겨지고 안 깨짐.
    assert "2601057" not in html and "[ID]" in html
    assert "user@test.ac.kr" not in html and "[이메일]" in html
    assert "010-1234-5678" not in html and "[전화]" in html
    assert "1234567" not in html and "[이름]([ID])" in html
    assert "홍길동" not in html   # 본인 이름(프로필에서 추출)
    assert "2601057" not in html  # title 속성의 학번도 마스킹(속성도 처리)
    # 각괄호 플레이스홀더가 남아 브라우저가 숨기는 일이 없어야 함
    assert "<ID>" not in html and "<EMAIL>" not in html


def test_hidden_and_password_values_cleared(captured):
    html, _ = captured
    assert "should-be-cleared" not in html


def test_self_contained_no_external_refs(captured):
    html, _ = captured
    # 외부 http 참조가 리소스로 남지 않아야(앵커 href 제외는 허용) 오프라인 재현
    assert 'href="/style.css"' not in html
    assert 'src="/img.png"' not in html


# --- 크롤러 연결부 -----------------------------------------------------
class _Shim:
    def __init__(self, page):
        self.page = page
        self.timeout_ms = 5000


def _crawler(browser, server, tmp_path, *, capture_html=True):
    from syncnote.eclass import crawler as C
    from syncnote.eclass.urls import Site
    from syncnote.paths import Layout
    from syncnote.state import Store

    pg = browser.new_page()
    pg.goto(f"{server}/page.html", wait_until="domcontentloaded")
    c = C.Crawler(_Shim(pg), Site(), Layout(tmp_path), Store.open(tmp_path / "s.json"),
                  term="2026-2", menus={}, screenshots=False, capture_html=capture_html)
    return c, pg


def test_snapshot_writes_full_html_next_to_md(browser, server, tmp_path):
    c, pg = _crawler(browser, server, tmp_path)
    try:
        md = tmp_path / "과목정보.md"
        md.write_text("# 과목정보\n", encoding="utf-8")
        c._snapshot(md)
        html = md.with_suffix(".html")
        assert html.exists(), "전체 HTML(.html) 이 .md 옆에 안 생겼습니다"
        assert c.stats.html_saved == 1
        assert "data:image/png;base64," in html.read_text(encoding="utf-8")
    finally:
        pg.close()


def test_capture_html_can_be_disabled(browser, server, tmp_path):
    c, pg = _crawler(browser, server, tmp_path, capture_html=False)
    try:
        md = tmp_path / "x.md"
        md.write_text("# x\n", encoding="utf-8")
        c._snapshot(md)
        assert not md.with_suffix(".html").exists()
        assert c.stats.html_saved == 0
    finally:
        pg.close()
