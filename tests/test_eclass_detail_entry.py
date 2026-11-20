"""상세 진입과 첨부 요청 헤더.

2026-11-13 실행에서 게시글 35건 중 26건이 "첨부 링크를 못 찾았습니다"
로 끝났습니다. 진단 덤프를 보니 그 화면에 onclick 을 가진 요소가
홈으로 가는 링크 하나뿐이었습니다 — 상세로 **들어가지도 못한** 것이고,
오류 메시지는 전혀 다른 곳을 가리키고 있었습니다.

원인: 이 LMS 는 글 열람을 JS 폼 POST 로 합니다. 클릭 직후에는 아직
이전 페이지가 domcontentloaded 상태라 wait_for_load_state 가 즉시
돌아옵니다. 그래서 옛 목록 화면을 읽습니다.

이 파일의 서버는 **진짜 폼 POST** 를 받습니다. 정적 픽스처만으로는
이 버그가 재현되지 않습니다 — 그래서 지난번에 못 잡았습니다.
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

from syncnote.eclass import crawler as C  # noqa: E402

CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]

# 테스트가 서버 동작을 바꿉니다.
STATE: dict = {"filedown_403_until": 0, "seen_headers": []}

LIST_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<form name="boardForm" method="post" action="/detail">
  <input type="hidden" name="boardContentsId" value="">
</form>
<table class="boardListBasic"><tbody>
  <tr class="boardRow">
    <td class="textLeft"><a href="javascript:moveBoardView('BOAD_1','2');"
           onclick="document.boardForm.boardContentsId.value='BOAD_1';
                    document.boardForm.submit(); return false;">공지 제목</a></td>
  </tr>
</tbody></table>
<a href="/Main.do?cmd=viewHome">홈</a>
</body></html>"""

# 상세 화면에만 첨부가 있습니다. 목록을 읽으면 0개입니다.
DETAIL_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div class="boardViewBox">본문입니다.</div>
<span onclick="fileDownload('보고서.hwp','COMF_1.hwp','*2Fboard*2F');">보고서.hwp</span>
<a href="/Main.do?cmd=viewHome">홈</a>
</body></html>"""

# 눌러도 아무 데도 안 가는 화면(navigation 없음).
DEAD_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<table class="boardListBasic"><tbody><tr class="boardRow">
  <td class="textLeft"><a href="#" onclick="return false;">아무 일도 안 일어남</a></td>
</tr></tbody></table>
</body></html>"""


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a, **k):
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._send(DETAIL_HTML.encode(), "text/html; charset=utf-8")

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/fileDownServlet"):
                # 헤더 이름의 대소문자는 서버마다 다르게 옵니다.
                STATE["seen_headers"].append(
                    {k.lower(): v for k, v in self.headers.items()})
                if STATE["filedown_403_until"] > 0:
                    STATE["filedown_403_until"] -= 1
                    self._send(b"denied", "text/plain", status=403)
                    return
                self._send(b"HWP-BYTES", "application/octet-stream")
                return
            if self.path.startswith("/dead"):
                self._send(DEAD_HTML.encode(), "text/html; charset=utf-8")
                return
            self._send(LIST_HTML.encode(), "text/html; charset=utf-8")

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", port), functools.partial(Handler))
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


class _Shim:
    def __init__(self, page):
        self.page = page
        self.timeout_ms = 4000
        self.logins = 0

    def login(self, *a, **kw):
        self.logins += 1


@pytest.fixture
def crawler(browser, server, tmp_path):
    from syncnote.eclass.urls import Site
    from syncnote.paths import Layout
    from syncnote.state import Store

    STATE["filedown_403_until"] = 0
    STATE["seen_headers"] = []
    pg = browser.new_page()
    pg.goto(server + "/")
    c = C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                  Store.open(tmp_path / "state.json"), term="2026-2", menus={})
    yield c
    pg.close()


def _link(crawler):
    return crawler.br.page.locator("tr.boardRow td.textLeft a").first


# --- 상세 진입 --------------------------------------------------------
def test_entering_detail_waits_for_the_form_post(crawler, tmp_path):
    """이게 핵심입니다. 기다리지 않으면 목록 화면을 읽습니다."""
    assert crawler._attachment_locators() is None, "목록 화면에 첨부가 있으면 안 됩니다"
    assert crawler._enter_detail(_link(crawler), "공지") is True
    links = crawler._attachment_locators()
    assert links is not None and links.count() == 1, "상세의 첨부를 못 찾았습니다"


# 참고: "예전 방식(클릭 + wait_for_load_state)" 이 틀리는 것을 여기서
# 재현하려고 했지만 **로컬에서는 재현되지 않습니다.** 127.0.0.1 폼 POST 는
# 1ms 안에 끝나서, 기다리지 않아도 이미 상세가 그려져 있습니다. 실제
# 서버에서만 그 틈이 벌어집니다.
#
# 이것이 이 회귀가 빠져나간 경로 그 자체입니다 — 정적 픽스처와 로컬
# 서버로는 타이밍 버그를 못 잡습니다. 그래서 아래는 '기다린다' 는
# 수단 자체를 고정합니다. 동작으로 못 잡으면 수단으로 잡습니다.
# 기다리는 방법은 여러 가지입니다. 하나만 박아 두면 나중에 똑같이
# 올바른 리팩터가 테스트를 빨갛게 만듭니다. 목록으로 둡니다.
#
# 여기에 뭔가를 추가할 때는 "그 호출이 **화면이 바뀔 때까지 막는가**"
# 를 기준으로 보세요. wait_for_load_state('domcontentloaded') 는
# 그렇지 않아서(이미 그 상태면 즉시 반환) 목록에 없습니다. 그게 바로
# 이 버그였습니다.
#
# 기준을 만족하지만 아직 안 쓰는 것도 있습니다. 폼 POST 가 아니라
# 화면을 갈아끼우는 방식으로 바뀌면
#   page.wait_for_selector(".boardViewBox", state="visible")
# 가 맞습니다. 그때 이 목록에 넣으세요.
#
# 다만 **맨 `wait_for_selector` 는 넣으면 안 됩니다.** 목록 행처럼
# 이미 있는 요소를 기다리면 즉시 반환이라, 이 버그가 그대로 통과합니다.
# 상세 화면에만 있는 선택자여야 의미가 있어서, 문자열 검사로는
# 구분할 수 없습니다. 그래서 지금은 목록에 없습니다.
WAITS_FOR_NAVIGATION = (
    "expect_navigation",
    "wait_for_url",
    "wait_for_function",
    "wait_for_load_state(\"networkidle\")",
    "wait_for_load_state('networkidle')",
)


def test_enter_detail_actually_waits_for_navigation():
    """화면이 바뀔 때까지 막는 호출이 있는지 소스로 확인합니다.

    로컬 127.0.0.1 에서는 기다리든 말든 둘 다 초록이라, 이 검사를
    빼면 회귀가 그대로 통과합니다. 실제 서버에서만 터집니다.
    """
    import inspect

    src = inspect.getsource(C.Crawler._enter_detail)
    assert any(w in src for w in WAITS_FOR_NAVIGATION), (
        "클릭 뒤에 화면이 바뀔 때까지 막는 호출이 없습니다. "
        f"허용 목록: {WAITS_FOR_NAVIGATION}"
    )


def test_entering_detail_reports_failure_when_nothing_navigates(crawler, server):
    crawler.br.page.goto(server + "/dead")
    assert crawler._enter_detail(_link(crawler), "죽은 링크") is False


# --- 진입 실패와 첨부 못 찾음은 다른 말이어야 합니다 ------------------
def test_failure_message_says_which_of_the_two_it_was(crawler, tmp_path):
    md = tmp_path / "x.md"
    md.write_text("x", encoding="utf-8")
    empty = C.Attachments()

    crawler._finish_post(gkey := "k1", md, empty, expected_file=True,
                         what="공지", extra={}, entered=False)
    assert any("상세 화면에 못 들어갔습니다" in e for e in crawler.stats.errors), \
        crawler.stats.errors
    assert not crawler.store.is_downloaded(gkey), "실패인데 완료로 적혔습니다"

    crawler.stats.errors.clear()
    crawler._finish_post("k2", md, empty, expected_file=True,
                         what="공지", extra={}, entered=True)
    assert any("링크를 찾지 못했습니다" in e for e in crawler.stats.errors), \
        crawler.stats.errors


# --- 첨부 요청 헤더 ---------------------------------------------------
def test_filedown_sends_a_referer(crawler, tmp_path):
    """page.request 는 Referer 를 안 보냅니다. 우리가 넣어야 합니다.

    Referer 를 요구하는 서버(직링크 차단)는 403 이 아니라 거부 안내
    HTML 을 줍니다. 그러면 우리는 '세션 만료' 로 오진합니다.
    """
    crawler._enter_detail(_link(crawler), "공지")
    got = crawler._download_attachments(tmp_path / "att")
    assert len(got.saved) == 1, (got.saved, got.failed)
    assert STATE["seen_headers"], "서버가 요청을 못 받았습니다"
    head = STATE["seen_headers"][-1]
    assert head.get("referer"), f"Referer 가 없습니다: {head}"


def test_page_request_already_carries_the_browser_user_agent(crawler, tmp_path):
    """UA 는 건드리지 않습니다 — 이미 브라우저 것이 나갑니다.

    "page.request 는 Playwright/x.y UA 로 나간다" 는 흔한 오해입니다.
    그건 독립 컨텍스트(playwright.request.new_context()) 쪽 이야기고,
    page.request 는 브라우저 컨텍스트의 UA 를 그대로 씁니다.
    이 테스트는 그 사실이 바뀌면 알려 줍니다 — 바뀌면 그때 UA 를
    직접 넣어야 합니다.
    """
    crawler._enter_detail(_link(crawler), "공지")
    crawler._download_attachments(tmp_path / "att")
    ua = STATE["seen_headers"][-1].get("user-agent", "")
    assert ua and "Playwright" not in ua, f"UA 를 직접 넣어야 합니다: {ua!r}"


def test_filedown_re_logs_in_once_on_403(crawler, tmp_path):
    """순회가 길어지면 중간에 세션이 떨어집니다. 한 번은 다시 잡습니다."""
    STATE["filedown_403_until"] = 1
    crawler._enter_detail(_link(crawler), "공지")
    got = crawler._download_attachments(tmp_path / "att")
    assert crawler.br.logins == 1, f"재로그인 횟수 {crawler.br.logins}"
    assert len(got.saved) == 1, (got.saved, got.failed)


def test_filedown_gives_up_after_one_retry(crawler, tmp_path):
    """영원히 재시도하면 한 글에서 세션이 끝납니다."""
    STATE["filedown_403_until"] = 99
    crawler._enter_detail(_link(crawler), "공지")
    got = crawler._download_attachments(tmp_path / "att")
    assert not got.saved and got.failed
    assert crawler.br.logins == 1, f"재로그인 횟수 {crawler.br.logins}"


# --- 진단 덤프 --------------------------------------------------------
def test_diagnostics_name_the_frames(crawler):
    """0개로 끝났을 때 '프레임이 없다' 와 '프레임 안에도 없다' 는 다릅니다."""
    text = crawler.describe_attachment_candidates()
    assert "[프레임" in text, text
    assert "fileDown=" in text, text
    assert "현재 http" in text, text


# --- 진입 실패의 '결과' 를 검사합니다 (C4 / C8) ------------------------
# 기존 테스트는 _finish_post 를 직접 불러 로그 문구만 봤습니다. 그래서
# _open_post 가 entered 를 **안 넘겨도** 전부 초록이었습니다. 그러면
# 26건을 "링크를 못 찾았다" 로 잘못 진단한 사고가 그대로 재발합니다.
#
# 로그 문구가 아니라 부작용을 봅니다 — docs/TESTING.md 원칙 5.
def _outdir(tmp_path):
    d = tmp_path / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _row():
    return {"index": 0, "no": "1", "title": "공지 제목", "date": "2026-11-13",
            "has_file": True, "author": "이교수", "cells": []}


def test_open_post_reports_the_entry_failure_not_a_missing_link(
        crawler, tmp_path, monkeypatch, server):
    """entered 를 안 넘기면 원인이 통째로 바뀝니다(변이 C4)."""
    crawler.br.page.goto(server + "/dead")
    monkeypatch.setattr(crawler, "_download_attachments",
                        lambda d: C.Attachments())
    crawler._open_post(C.Course("X", "과목가"), "notice", _row(),
                       _outdir(tmp_path), "key-c4")
    joined = " ".join(crawler.stats.errors)
    assert "상세 화면에 못 들어갔습니다" in joined, joined
    assert "링크를 찾지 못했습니다" not in joined, joined


def test_open_post_keeps_the_post_pending_when_entry_fails(
        crawler, tmp_path, monkeypatch, server):
    """진입 실패를 '완료' 로 적으면 다음 실행이 건너뜁니다."""
    crawler.br.page.goto(server + "/dead")
    monkeypatch.setattr(crawler, "_download_attachments",
                        lambda d: C.Attachments())
    crawler._open_post(C.Course("X", "과목가"), "notice", _row(),
                       _outdir(tmp_path), "key-c4b")
    assert not crawler.store.is_downloaded("key-c4b")


def test_open_message_reports_the_entry_failure_too(
        crawler, tmp_path, monkeypatch, server):
    """쪽지 쪽에서 같은 사고입니다(변이 C8)."""
    crawler.br.page.goto(server + "/dead")
    monkeypatch.setattr(crawler, "_download_attachments",
                        lambda d: C.Attachments())
    row = {"index": 0, "no": "1", "title": "쪽지 제목", "date": "2026-11-13",
           "has_file": True}
    crawler._open_message(row, "보낸이-abc123", _outdir(tmp_path),
                          "key-c8", "받은쪽지함")
    joined = " ".join(crawler.stats.errors)
    assert "상세 화면에 못 들어갔습니다" in joined, joined


def test_a_successful_entry_blames_the_selector_instead(
        crawler, tmp_path, monkeypatch):
    """진입은 됐는데 첨부가 0개면 그때는 선택자 문제입니다.

    두 메시지가 뒤바뀌면 다음 사람이 엉뚱한 곳을 고칩니다.
    """
    monkeypatch.setattr(crawler, "_download_attachments",
                        lambda d: C.Attachments())
    crawler._open_post(C.Course("X", "과목가"), "notice", _row(),
                       _outdir(tmp_path), "key-ok")
    joined = " ".join(crawler.stats.errors)
    assert "링크를 찾지 못했습니다" in joined, joined
    assert "상세 화면에 못 들어갔습니다" not in joined, joined


def test_a_post_leaves_a_screenshot_of_the_detail_page(crawler, tmp_path, monkeypatch):
    """폼 POST 로 들어간 상세 화면을 찍어야 합니다(목록 화면이 아니라)."""
    monkeypatch.setattr(crawler, "_download_attachments",
                        lambda d: C.Attachments())
    out = _outdir(tmp_path)
    crawler._open_post(C.Course("X", "과목가"), "notice", _row(), out, "key-shot")
    md = next(out.glob("*.md"))
    png = md.with_suffix(".png")
    assert png.exists(), "게시글 스크린샷이 없습니다"
    assert crawler.stats.shots == 1
