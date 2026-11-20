"""과제 '제출정보' 수집 — 과제명·제출기간·제출상태·참고자료·첨부파일.

과제 목록만으로는 과제명·제출기간·제출여부·참고자료까지입니다. '제출상태'
(제출완료/과제제출일시)와 **제출한 첨부파일** 은 '제출정보보기'
(submitReportView=열람 전용)로 들어가는 상세 화면에만 있습니다.

핵심 안전 원칙: 누르는 것은 submitReportView(열람)뿐입니다. 과제를 실제로
내는 submitReport(과제제출)/제출/goSave 는 절대 누르지 않습니다 — 글자가
'submitReport' 까지 겹치지만 'View(' 로 가릅니다. 아래 전체 루프 테스트는
**진짜 폼 POST** 를 받아 cmd 가 늘 viewReportSubmit 인지(제출 경로가 아닌지)
서버에서 확인합니다.
"""

from __future__ import annotations

import functools
import http.server
import os
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from syncnote.eclass import crawler as C  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
LIST = "eclass_assignment_submit_list.html"
DETAIL = "eclass_assignment_submit_detail.html"
CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]


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
        self.timeout_ms = 5000
        self.logins = 0

    def login(self, *a, **kw):
        self.logins += 1


def _mk(browser, tmp_path, url):
    from syncnote.eclass.urls import Site
    from syncnote.paths import Layout
    from syncnote.state import Store

    pg = browser.new_page()
    pg.goto(url)
    return C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "state.json"), term="2026-2", menus={})


# --- 정적 픽스처: 목록 파싱 -------------------------------------------
@pytest.fixture(scope="module")
def static_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {
            **http.server.SimpleHTTPRequestHandler.extensions_map,
            ".html": "text/html; charset=utf-8",
        }

        def log_message(self, *a, **k):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(Handler, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def on_list(browser, static_server, tmp_path):
    c = _mk(browser, tmp_path, f"{static_server}/{LIST}")
    yield c
    c.br.page.close()


@pytest.fixture
def on_detail(browser, static_server, tmp_path):
    c = _mk(browser, tmp_path, f"{static_server}/{DETAIL}")
    yield c
    c.br.page.close()


def test_entries_carry_title_due_status(on_list):
    entries = on_list.read_assignment_entries()
    assert len(entries) == 3, entries
    titles = [e["title"] for e in entries]
    assert any("Idea Map Worksheet" in t for t in titles), titles
    # 제목에 '제출정보보기'/'과제제출' 버튼 글자가 섞이면 안 됩니다.
    assert not any("제출정보보기" in t or "과제제출" in t for t in titles), titles
    first = entries[0]
    assert first["due"] == "2026-11-05 09:10 ~ 2026-11-11 23:59", first
    assert first["status"] == "제출완료", first


def test_only_submitted_entries_get_a_report_id(on_list):
    """제출완료(submitReportView) 만 reportId 를 가집니다.

    미제출(submitReport=과제제출, 위험) 은 상세가 없으므로 report_info_id 가
    비어 상세 수집 대상에서 빠집니다.
    """
    entries = on_list.read_assignment_entries()
    ids = [e["report_info_id"] for e in entries]
    assert ids == ["REPT_AAA000", "", "REPT_CCC222"], ids
    # detail_index 는 '제출정보보기' 가 걸린 순서(nth)여야 합니다.
    assert [e["detail_index"] for e in entries] == [0, -1, 1]


def test_view_selector_never_matches_the_submit_button(on_list):
    """submitReportView(열람) 와 submitReport(과제제출) 를 확실히 가릅니다."""
    page = on_list.br.page
    assert page.locator(C.ASSIGN_DETAIL_LINK).count() == 2
    # 'submitReport(' 는 'submitReportView(' 의 부분문자열이 아닙니다.
    dangerous = page.locator("a[onclick*='submitReport(']")
    assert dangerous.count() == 1, "과제제출 버튼이 1개여야 합니다"
    oc = dangerous.first.get_attribute("onclick")
    assert "submitReportView(" not in oc, oc
    assert C._is_unsafe_click(oc), "과제제출은 위험으로 분류돼야 합니다"


# --- 정적 픽스처: 상세 파싱 -------------------------------------------
def test_detail_pairs_cover_every_requested_field(on_detail):
    pairs = on_detail.read_submission_detail()
    fields = dict(pairs)
    assert fields.get("과제명") == "10/8(목) Presentation Script 제출", fields
    assert fields.get("제출기간") == "2026-10-08 00:00 ~ 2026-10-14 23:59", fields
    assert fields.get("제출상태") == "제출완료", fields
    assert "2026-10-10 14:22" in fields.get("과제제출일시", ""), fields
    assert fields.get("제출자") == "홍길동(0000000)", fields
    assert "Script.docx" in fields.get("첨부파일", "")
    assert "Notes.jpeg" in fields.get("첨부파일", "")


def test_detail_attachments_are_spans_to_the_servlet(on_detail):
    links = on_detail._attachment_locators()
    assert links is not None, "첨부를 찾지 못했습니다"
    # 참고자료 1 + 제출첨부 2 = 3 개의 fileDownload span.
    assert links.count() == 3, links.count()
    assert links.first.evaluate("el => el.tagName") == "SPAN"


def test_submission_section_lists_the_fields():
    entry = {"title": "발표 대본", "due": "2026-10-08 00:00 ~ 2026-10-14 23:59",
             "status": "제출완료"}
    pairs = [("과제명", "발표 대본"), ("제출기간", "2026-10-08 00:00 ~ 2026-10-14 23:59"),
             ("제출자", "홍길동(0000000)"), ("첨부파일", "Script.docx Notes.jpeg"),
             ("제출상태", "제출완료"), ("과제제출일시", "2026-10-10 14:22"),
             ("과제설명", "발표 대본 초안입니다.")]
    md = C.Crawler._submission_section("발표 대본", entry, pairs)
    assert "## 발표 대본" in md
    assert "- 제출기간: 2026-10-08 00:00 ~ 2026-10-14 23:59" in md
    assert "- 제출상태: 제출완료" in md
    assert "- 첨부파일: Script.docx Notes.jpeg" in md
    assert "- 과제제출일시: 2026-10-10 14:22" in md
    assert "### 과제설명" in md and "발표 대본 초안입니다." in md


# --- 전체 루프: 진짜 폼 POST + 안전성 ---------------------------------
STATE: dict = {"posts": [], "downloads": 0}


@pytest.fixture(scope="module")
def post_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    list_html = (FIXTURES / LIST).read_text(encoding="utf-8")
    detail_html = (FIXTURES / DETAIL).read_text(encoding="utf-8")

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
            body = self.rfile.read(length).decode("utf-8", "replace")
            cmd = (parse_qs(body).get("cmd") or [""])[0]
            STATE["posts"].append(cmd)
            # 열람(viewReportSubmit)만 상세를 줍니다. 그 외 cmd 가 오면
            # 우리 코드가 제출 경로를 누른 것 — 테스트가 그걸 잡습니다.
            self._send(detail_html.encode(), "text/html; charset=utf-8")

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/fileDownServlet"):
                STATE["downloads"] += 1
                self._send(b"FILE-" + self.path.encode(), "application/octet-stream")
                return
            self._send(list_html.encode(), "text/html; charset=utf-8")

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", port), functools.partial(Handler))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture
def looped(browser, post_server, tmp_path, monkeypatch):
    STATE["posts"] = []
    STATE["downloads"] = 0
    c = _mk(browser, tmp_path, post_server + "/list")
    # 상세를 한 번 들어가면 목록이 사라집니다. 실제 사이트는 open_menu 가
    # Main.do 로 다시 가지만, 테스트에서는 목록 픽스처로 돌려보냅니다.
    monkeypatch.setattr(
        c, "_return_to_assignment_list",
        lambda course: (c.br.page.goto(post_server + "/list") or True))
    yield c
    c.br.page.close()


def test_loop_collects_details_and_downloads_submissions(looped, tmp_path):
    course = C.Course("2026210217623", "영어2_23", "송교수")
    done, nfiles = looped.collect_assignment_submissions(course, tmp_path / "과제")
    assert done == 2, "제출완료 과제 2건의 상세를 읽어야 합니다"
    # 상세마다 참고자료 1 + 제출첨부 2 = 3개씩, 중복은 내용해시로 걸러집니다.
    assert nfiles >= 2, nfiles
    md = next(tmp_path.rglob("과제_제출정보.md"), None)
    assert md is not None, "과제_제출정보.md 가 없습니다"
    text = md.read_text(encoding="utf-8")
    assert text.count("\n## ") == 2, text
    assert "제출상태: 제출완료" in text
    # 제출한 첨부파일은 '제출첨부' 아래 과제별 폴더로 받습니다.
    assert any(p.name == "제출첨부" and p.is_dir()
               for p in (tmp_path / "과제").rglob("제출첨부")), \
        "제출첨부 폴더가 없습니다"
    assert STATE["downloads"] >= 2, "첨부 다운로드 요청이 없었습니다"


def test_loop_only_ever_posts_the_view_command(looped, tmp_path):
    """가장 중요한 안전성 검사: 제출 경로 cmd 가 한 번도 안 나가야 합니다."""
    looped.collect_assignment_submissions(
        C.Course("2026210217623", "영어2_23", "송교수"), tmp_path / "과제")
    assert STATE["posts"], "폼 POST 가 한 번도 없었습니다(상세 진입 실패)"
    assert set(STATE["posts"]) == {"viewReportSubmit"}, STATE["posts"]
    assert "addReportSubmit" not in STATE["posts"]
    assert "viewReportSubmitForm" not in STATE["posts"]
