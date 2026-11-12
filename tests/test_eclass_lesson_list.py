"""실제 '강의 목록'·게시판 페이지(익명화본)로 첨부 수집을 검증합니다.

fixtures/eclass_lesson_list.html   — /Lesson.do viewLessonContentsList
fixtures/eclass_board_list_real.html — 글 한 건 있는 게시판 목록
fixtures/eclass_board_empty.html     — 글이 없는 게시판 목록

전부 2026-11 에 받은 실제 페이지에서 사람 이름·학번·이메일·과목명만
가명으로 바꾼 것입니다. 구조와 JS 는 손대지 않았습니다.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import socket
import threading
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from gymhall.eclass import crawler as C  # noqa: E402
from gymhall.eclass import filedown as F  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
# 테스트 서버가 /fileDown 에 무엇으로 답할지. 테스트가 바꿉니다.
SERVER_MODE = {"mode": "pdf"}
LESSON = "eclass_lesson_list.html"
BOARD = "eclass_board_list_real.html"
EMPTY = "eclass_board_empty.html"
CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]


# --- fileDown 해석(브라우저 불필요) -----------------------------------
REAL_ONCLICK = (
    "fileDown('1*EC*9E*A5_*EC*9B*B9*ED*94*84*EB*A1*9C*EA*B7*B8*EB*9E*98*EB*B0*8D.pdf',"
    "'1*EC*9E*A5_*EC*9B*B9*ED*94*84*EB*A1*9C*EA*B7*B8*EB*9E*98*EB*B0*8D.pdf',"
    "'*2Flmsdata*2Fcontents*2F2026211605005*2F1*EC*9E*A5.pdf');"
)


def test_filedown_decodes_korean_names():
    call = F.parse(REAL_ONCLICK)
    assert call is not None
    assert F.filename(call) == "1장_웹프로그래밍.pdf"
    assert F.decode(call.path) == "/lmsdata/contents/2026211605005/1장.pdf"


def test_filedown_url_keeps_the_server_encoding():
    """인자를 다시 인코딩하면 '*' 가 '%2A' 가 되어 다른 파일을 찾습니다."""
    url = F.url("https://eclass.kunsan.ac.kr/Lesson.do?cmd=x", F.parse(REAL_ONCLICK))
    assert url.startswith("https://eclass.kunsan.ac.kr/fileDown?")
    assert "*EC*9E*A5" in url
    assert "%2A" not in url


def test_the_two_download_functions_use_different_endpoints():
    """이름이 비슷하지만 주소가 다릅니다. 섞으면 404 가 납니다.

        FileDown.js      fileDown(...)      -> fileDown?...
        FileDownload.js  fileDownload(...)  -> fileDownServlet?...

    두 JS 원문을 모두 받아 확인한 값입니다.
    """
    down = F.parse("fileDown('a','b','c')")
    load = F.parse("fileDownload('a','b','c')")
    assert down.fn == "fileDown" and down.endpoint == "fileDown"
    assert load.fn == "fileDownload" and load.endpoint == "fileDownServlet"
    base = "https://eclass.kunsan.ac.kr/Course.do?x=1"
    assert F.url(base, down).startswith("https://eclass.kunsan.ac.kr/fileDown?")
    assert F.url(base, load).startswith("https://eclass.kunsan.ac.kr/fileDownServlet?")


def test_filedownload_is_not_read_as_filedown():
    """'fileDownload' 안에 'fileDown' 이 들어 있습니다.

    짧은 쪽으로 잘못 읽히면 fileDown 엔드포인트로 가서 404 가 납니다.
    (이름 뒤에 '(' 를 요구하므로 교대 순서와는 무관합니다 — 확인했습니다.)
    """
    call = F.parse("fileDownload('a','b','c')")
    assert call.fn == "fileDownload", call
    assert call.endpoint == "fileDownServlet"


def test_a_bare_function_name_without_parens_is_not_a_call():
    """'(' 요구가 빠지면 fileDownload 를 fileDown 으로 읽게 됩니다."""
    assert F.parse("fileDownload") is None
    assert F.parse("var x = fileDown;") is None


def test_filedown_handles_spaces_and_parens():
    """실제 파일명에 공백(*20)과 괄호(*28/*29)가 들어 있습니다."""
    call = F.parse("fileDown('2*EC*9E*A5_HTML5*20*EA*B8*B0*EB*B3*B8*281*29.pdf','x','y')")
    assert F.filename(call) == "2장_HTML5 기본(1).pdf"


def test_filedown_ignores_a_non_download_onclick():
    assert F.parse("javascript:moveBoardView('BOAD_1','2');") is None
    assert F.parse("javascript:moveMessageView('MESG_1','MESG_2');") is None
    assert F.parse("") is None


def test_filedown_falls_back_when_the_display_name_is_empty():
    """게시판 첨부는 보여 줄 이름이 비어도 저장명이 있습니다."""
    call = F.parse("fileDownload('','COMF_26083020.hwp','*2Fbusiness*2Fboard*2Fx*2F')")
    assert F.filename(call) == "COMF_26083020.hwp"
    call = F.parse("fileDown('','','*2Fa*2Fb*2F1*EC*9E*A5.pdf')")
    assert F.filename(call) == "1장.pdf"


def test_filedown_js_in_the_fixture_still_matches_our_parser():
    """사이트의 FileDown.js 가 바뀌면 우리 주소 조립도 틀려집니다."""
    html = (FIXTURES / LESSON).read_text(encoding="utf-8")
    calls = re.findall(r'onclick="(fileDown\(.*?\));?"', html)
    assert len(calls) == 14, f"교안 링크가 14개가 아닙니다: {len(calls)}"
    assert all(F.parse(c) is not None for c in calls)
    names = [F.filename(F.parse(c)) for c in calls]
    assert all(n.lower().endswith(".pdf") for n in names), names


# --- 브라우저 ---------------------------------------------------------
@pytest.fixture(scope="module")
def server():
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

        def log_error(self, *a, **k):
            pass

        def do_GET(self):  # noqa: N802
            # 사이트의 /fileDown 흉내. MODE 로 응답을 바꿔 가며
            # 정상·세션만료·빈 파일 세 경우를 전부 시험합니다.
            if not self.path.startswith("/fileDown?"):
                super().do_GET()
                return
            mode = SERVER_MODE["mode"]
            if mode == "html":
                body, ctype = b"<html><body>\xeb\xa1\x9c\xea\xb7\xb8\xec\x9d\xb8</body></html>", "text/html; charset=utf-8"
            elif mode == "empty":
                body, ctype = b"", "application/pdf"
            else:
                # 파일마다 내용이 달라야 합니다. 전부 같은 바이트를 주면
                # 내용 해시 중복 제거에 걸려 1개만 남습니다.
                body = b"%PDF-1.4 " + self.path.encode()
                ctype = "application/pdf"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    handler = functools.partial(Handler, directory=str(FIXTURES))

    class Quiet(http.server.ThreadingHTTPServer):
        def handle_error(self, *a):
            pass

    httpd = Quiet(("127.0.0.1", port), handler)
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
        self.timeout_ms = 5000


def _crawler(browser, server, tmp_path, fixture):
    from gymhall.eclass.urls import Site
    from gymhall.paths import Layout
    from gymhall.state import Store

    pg = browser.new_page()
    pg.goto(f"{server}/{fixture}")
    return C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "state.json"), term="2026-2", menus={})


@pytest.fixture
def lesson(browser, server, tmp_path):
    c = _crawler(browser, server, tmp_path, LESSON)
    yield c
    c.br.page.close()


def test_attachment_selector_finds_every_lesson_pdf(lesson):
    """예전 선택자 여섯 개는 이 페이지에서 전부 0개를 잡았습니다.

    'fileDown' 에는 'ownload' 가 들어 있지 않아서
    a[onclick*='ownload'] 로는 영영 못 찾습니다. 교안 14개를
    한 장도 못 받고 있었고, 오류도 경고도 나지 않았습니다.
    """
    links = lesson._attachment_locators()
    assert links is not None, "첨부 링크를 하나도 찾지 못했습니다"
    assert links.count() == 14, links.count()


def test_old_selectors_really_matched_nothing(lesson):
    """회귀 방지 — 폴백만 남기면 다시 0개가 되는지 확인합니다."""
    page = lesson.br.page
    old = ("a[onclick*='ownload']", "a[href*='ownload']",
           "ul.fileList a", "div.fileBox a", "td.file a", "a.fileDown")
    assert sum(page.locator(sel).count() for sel in old) == 0


def test_lesson_weeks_are_read_per_week(lesson):
    weeks = lesson.read_lesson_weeks()
    assert len(weeks) == 16, len(weeks)
    assert weeks[0]["title"].startswith("1. 1주차"), weeks[0]["title"]
    assert any(w["rows"] for w in weeks), "차시가 한 건도 없습니다"


def test_lesson_rows_carry_the_session_name(lesson):
    weeks = lesson.read_lesson_weeks()
    cells = [c for w in weeks for row in w["rows"] for c in row]
    assert any("HTML5" in c for c in cells), "차시명을 읽지 못했습니다"


def test_crawl_lecture_list_writes_markdown_with_every_week(lesson, tmp_path):
    course = C.Course("2026211605005", "과목가", "이교수")
    lesson.crawl_lecture_list(course)
    md = next(tmp_path.rglob("강의목록.md"), None)
    assert md is not None, "강의목록.md 가 만들어지지 않았습니다"
    text = md.read_text(encoding="utf-8")
    assert text.count("\n## ") == 16, "주차 16개가 다 들어가지 않았습니다"


def test_filedown_actually_saves_the_pdf(lesson, tmp_path):
    """클릭이 아니라 주소로 직접 받아 파일이 디스크에 남아야 합니다."""
    SERVER_MODE["mode"] = "pdf"
    saved = lesson._download_attachments(tmp_path / "att").saved
    assert len(saved) == 14, [p.name for p in saved]
    assert all(p.read_bytes().startswith(b"%PDF") for p in saved)
    names = {p.name for p in saved}
    assert any(n.startswith("1장_") for n in names), names


def test_html_instead_of_a_file_is_refused(lesson, tmp_path):
    """세션이 끊기면 서버가 파일 대신 로그인 화면을 줍니다.

    그걸 .pdf 로 저장하면 사용자는 깨진 파일 14개를 받고, 상태 파일에는
    '받음' 으로 기록돼 다음 실행에서 다시 받지도 않습니다.
    """
    SERVER_MODE["mode"] = "html"
    try:
        saved = lesson._download_attachments(tmp_path / "att").saved
    finally:
        SERVER_MODE["mode"] = "pdf"
    assert saved == [], [p.name for p in saved]
    assert lesson.stats.errors, "조용히 넘어갔습니다"
    assert not list((tmp_path / "att").glob("*.pdf"))


def test_zero_byte_response_is_refused(lesson, tmp_path):
    SERVER_MODE["mode"] = "empty"
    try:
        saved = lesson._download_attachments(tmp_path / "att").saved
    finally:
        SERVER_MODE["mode"] = "pdf"
    assert saved == []


def test_same_file_is_not_downloaded_twice(lesson, tmp_path):
    SERVER_MODE["mode"] = "pdf"
    first = lesson._download_attachments(tmp_path / "a").saved
    second = lesson._download_attachments(tmp_path / "b").saved
    assert len(first) == 14
    # 내용이 같으므로 두 번째는 전부 건너뜁니다.
    assert second == [], [p.name for p in second]


# --- 게시판 목록 ------------------------------------------------------
@pytest.fixture
def board(browser, server, tmp_path):
    c = _crawler(browser, server, tmp_path, BOARD)
    yield c
    c.br.page.close()


def test_board_rows_are_read_from_the_real_table(board):
    rows = board.read_board_rows()
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["title"] == "중간고사", row
    assert row["no"] == "1"
    assert row["date"] == "2026-10-07"
    assert row["author"] == "이교수"
    assert row["has_file"] is False, "첨부가 없는 글인데 있다고 봤습니다"


def test_board_title_link_selector_matches(board):
    loc = board.br.page.locator(C.BOARD_ROW).first.locator(C.BOARD_TITLE_LINK).first
    assert loc.count() == 1
    href = loc.get_attribute("href")
    assert href.startswith("javascript:moveBoardView("), href


def test_move_board_view_posts_the_contents_id(board):
    """목록에서 상세로 가는 길이 폼 POST 라는 전제를 고정합니다."""
    html = (FIXTURES / BOARD).read_text(encoding="utf-8")
    body = re.search(r"function moveBoardView\(.*?\n\t\}", html, re.S).group(0)
    assert 'f["cmd"].value = "viewBoardContents"' in body
    assert 'boardContentsDTO.boardContentsId' in body
    assert "f.submit()" in body


def test_single_page_board_reports_one_page(board):
    assert board.board_page_count() == 1


@pytest.fixture
def empty(browser, server, tmp_path):
    c = _crawler(browser, server, tmp_path, EMPTY)
    yield c
    c.br.page.close()


def test_empty_board_yields_no_rows(empty):
    """'조회된 내용이 없습니다' 화면을 글 0건으로 읽어야 합니다."""
    assert empty.read_board_rows() == []
    assert "조회된 내용이 없습니다" in empty.br.page.inner_text("#listBox")


# --- 게시글 상세 ------------------------------------------------------
DETAIL = "eclass_board_detail.html"


@pytest.fixture
def detail(browser, server, tmp_path):
    c = _crawler(browser, server, tmp_path, DETAIL)
    yield c
    c.br.page.close()


def test_detail_attachment_is_a_span_not_a_link(detail):
    """게시글 첨부는 <a> 가 아니라 <span onclick="fileDownload(...)"> 입니다.

    'a' 로 시작하는 선택자만 두면 게시판 첨부를 한 건도 못 받습니다.
    강의 목록 쪽(<a onclick="fileDown(...)">)과 함수도 태그도 다릅니다.
    """
    links = detail._attachment_locators()
    assert links is not None, "첨부를 찾지 못했습니다"
    assert links.count() == 1
    assert links.first.evaluate("el => el.tagName") == "SPAN"


def test_only_a_selectors_find_nothing_on_the_detail_page(detail):
    page = detail.br.page
    only_anchors = ("a[onclick*='fileDown']", "a[onclick*='ownload']",
                    "a[href*='ownload']", "a.fileDown", "td.file a")
    assert sum(page.locator(sel).count() for sel in only_anchors) == 0


def test_detail_attachment_uses_the_servlet_endpoint(detail):
    """게시판 첨부는 fileDownServlet 로 갑니다(FileDownload.js 원문 기준).

    강의 목록 쪽 fileDown 과 섞으면 주소가 틀려 404 가 납니다.
    """
    onclick = detail._attachment_locators().first.get_attribute("onclick")
    call = F.parse(onclick)
    assert call is not None and call.fn == "fileDownload", onclick
    assert call.endpoint == "fileDownServlet"
    assert F.filename(call) == "보고서양식.hwp"
    assert F.decode(call.path) == "/business/board/2026210357604-N/"


def test_detail_attachment_name_is_korean(detail):
    """인코딩된 첫 인자가 사람이 읽는 이름으로 풀려야 합니다."""
    onclick = detail._attachment_locators().first.get_attribute("onclick")
    enc = re.search(r"fileDownload\('([^']*)'", onclick).group(1)
    assert F.decode(enc) == "보고서양식.hwp"


def test_detail_body_selector_picks_the_post_not_the_list(detail):
    """상세 화면 아래에는 글 목록이 또 붙어 있습니다.

    #bodyContent 를 통째로 읽으면 목록까지 본문에 섞여 들어갑니다.
    """
    page = detail.br.page
    body = ""
    for sel in C.DETAIL_BODY:
        loc = page.locator(sel).first
        if loc.count() and loc.inner_text().strip():
            body = loc.inner_text()
            break
    assert "강의자료 참고 사이트" in body, body[:200]
    assert "중간고사 안내" not in body, "아래 목록이 본문에 섞였습니다"


def test_detail_shows_author_and_date(detail):
    info = detail.br.page.locator("dd.info").first.inner_text()
    assert "작성자" in info and "조교수" in info
    assert "2026-08-30" in info


# --- 과제 ------------------------------------------------------------
ASSIGN = "eclass_assignment_list.html"


@pytest.fixture
def assignment(browser, server, tmp_path):
    c = _crawler(browser, server, tmp_path, ASSIGN)
    yield c
    c.br.page.close()


def test_assignment_page_is_not_a_boardlistbasic(assignment):
    """과제 목록은 게시판이 아닙니다.

    table.boardListBasic 이 아니라 dl.element + table.boardListInfo 라서
    crawl_board 로 읽으면 과제가 19개 있어도 0건으로 끝납니다.
    """
    page = assignment.br.page
    assert page.locator("table.boardListBasic").count() == 0
    assert page.locator(C.BOARD_ROW).count() == 0
    assert assignment.read_board_rows() == []


def test_assignment_blocks_are_read(assignment):
    blocks = assignment.read_lesson_weeks()
    assert len(blocks) == 19, len(blocks)
    titles = [b["title"] for b in blocks]
    assert any("파일 실습 과제(1)" in t for t in titles), titles[:3]


def test_assignment_title_excludes_the_submit_button_text(assignment):
    """dt 를 통째로 읽으면 제목에 '과제제출' 이 붙습니다."""
    titles = [b["title"] for b in assignment.read_lesson_weeks()]
    assert not any("과제제출" in t for t in titles), [t for t in titles if "과제제출" in t]


def test_assignment_rows_carry_the_due_date(assignment):
    cells = [c for b in assignment.read_lesson_weeks() for row in b["rows"] for c in row]
    assert any("~" in c and "2026-" in c for c in cells), cells[:5]


def test_submit_button_is_never_clicked(assignment, tmp_path):
    """과제 화면의 '과제제출' 을 누르면 되돌릴 수 없습니다.

    첨부 선택자가 이걸 잡는 일은 지금은 없지만, 폴백 선택자가 넓어서
    한 번만 어긋나면 빈 과제가 제출됩니다. 눌리는 자리에서 막습니다.
    """
    page = assignment.br.page
    submits = page.locator("a[onclick*='submitReport']")
    assert submits.count() >= 1, "픽스처에 과제제출 버튼이 없습니다"
    for i in range(submits.count()):
        onclick = submits.nth(i).get_attribute("onclick")
        assert C._is_unsafe_click(onclick), onclick
    saved = assignment._download_attachments(tmp_path / "att").saved
    assert saved == [], [p.name for p in saved]


def test_unsafe_handler_on_a_matched_attachment_is_skipped(detail, tmp_path):
    """첨부 선택자가 잡은 요소라도 되돌릴 수 없어 보이면 누르지 않습니다.

    이 가드는 '아직 본 적 없는 화면' 을 위한 것입니다. 지금 선택자가
    제출 버튼을 잡는 일은 없지만, 폴백 선택자(td.file a 등)가 넓어서
    한 번만 어긋나면 빈 과제가 제출됩니다. 그래서 실제 상세 화면의
    진짜 첨부 span 에 goSave() 를 덧붙여, 눌리는 자리에서 정말로
    막히는지 봅니다. 이 가드를 빼면 아래 단언이 깨집니다.
    """
    page = detail.br.page
    page.eval_on_selector(
        "[onclick*='fileDown']",
        "el => el.setAttribute('onclick', 'goSave();' + el.getAttribute('onclick'))",
    )
    before = len(detail.stats.errors)
    saved = detail._download_attachments(tmp_path / "att").saved
    assert saved == [], [p.name for p in saved]
    assert not list((tmp_path / "att").glob("*")), "누르지 말아야 할 것을 눌렀습니다"
    assert len(detail.stats.errors) == before


@pytest.mark.parametrize("handler", [
    "javascript:goSave();",
    "javascript:submitReport('REPT_1');",
    "javascript:uploaderclick('atchuploader');",
    "deleteBoardContents('BOAD_1','X')",
    "ViewEtestContent('ETST_1','menu_00104')",
])
def test_unsafe_handlers_are_recognised(handler):
    assert C._is_unsafe_click(handler), handler


@pytest.mark.parametrize("handler", [
    "fileDown('a','b','c')",
    "fileDownload('a','b','c')",
    "javascript:moveBoardView('BOAD_1','2');",
])
def test_download_handlers_are_allowed(handler):
    assert not C._is_unsafe_click(handler), handler


def test_crawl_assignments_writes_every_block(assignment, tmp_path):
    course = C.Course("2026210357604", "과목바", "조교수")
    assignment.crawl_assignments(course)
    md = next(tmp_path.rglob("과제.md"), None)
    assert md is not None, "과제.md 가 만들어지지 않았습니다"
    assert md.read_text(encoding="utf-8").count("\n## ") == 19


# --- 엔드포인트와 쿼리 인자를 못으로 박습니다 --------------------------
# 리뷰 지적: 완성된 URL 의 경로와 쿼리 인자를 각각 확인할 것.
def _q(url: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(url).query, keep_blank_values=True)


def test_filedown_url_carries_all_three_arguments_verbatim():
    call = F.parse("fileDown('r*20name.pdf','s*20name.pdf','*2Fa*2Fb.pdf')")
    url = F.url("https://eclass.kunsan.ac.kr/Lesson.do?cmd=x", call)
    from urllib.parse import urlsplit

    assert urlsplit(url).path == "/fileDown"
    q = _q(url)
    # 서버가 '*' 를 쓰는 인코딩을 그대로 받습니다. parse_qs 는 '*' 를
    # 건드리지 않으므로 값이 원문 그대로여야 합니다.
    assert q["rFileName"] == ["r*20name.pdf"]
    assert q["sFileName"] == ["s*20name.pdf"]
    assert q["filePath"] == ["*2Fa*2Fb.pdf"]


def test_filedownload_url_uses_the_servlet_path_with_the_same_arguments():
    call = F.parse("fileDownload('*EB*B3*B4.hwp','COMF_1.hwp','*2Fbusiness*2Fboard*2FX-N*2F')")
    url = F.url("https://eclass.kunsan.ac.kr/Course.do?cmd=x", call)
    from urllib.parse import urlsplit

    assert urlsplit(url).path == "/fileDownServlet"
    q = _q(url)
    assert q["rFileName"] == ["*EB*B3*B4.hwp"]
    assert q["sFileName"] == ["COMF_1.hwp"]
    assert q["filePath"] == ["*2Fbusiness*2Fboard*2FX-N*2F"]


def test_a_directory_path_is_never_used_as_the_file_name():
    """fileDownload 의 filePath 는 디렉터리입니다.

    경로의 마지막 조각으로 넘어가면 빈 이름이 나옵니다.
    """
    call = F.parse("fileDownload('','COMF_1.hwp','*2Fbusiness*2Fboard*2FX-N*2F')")
    assert F.filename(call) == "COMF_1.hwp"


def test_a_failed_download_is_reported_not_swallowed(lesson, tmp_path):
    """세션 만료(HTML 응답)면 실패 목록에 남아야 합니다."""
    SERVER_MODE["mode"] = "html"
    try:
        att = lesson._download_attachments(tmp_path / "att")
    finally:
        SERVER_MODE["mode"] = "pdf"
    assert att.saved == []
    assert len(att.failed) == 14, att.failed[:3]
    assert att.ok is False


def test_a_successful_download_reports_no_failure(lesson, tmp_path):
    SERVER_MODE["mode"] = "pdf"
    att = lesson._download_attachments(tmp_path / "att")
    assert len(att.saved) == 14
    assert att.failed == [] and att.ok is True


# --- 교안 0개일 때 단서를 남기는가 -------------------------------------
# 2026-11-13 수집에서 6과목이 "주차 16개인데 교안 0개" 로 끝났는데
# 로그에 단서가 한 줄도 없었습니다. 진단 덤프가 _finish_post(게시판
# 경로)에만 걸려 있었기 때문입니다. 강의목록은 상세 진입이 없는 단일
# 페이지라 그 경로를 안 탑니다.
#
# "교수가 안 올렸다" 와 "선택자가 어긋났다" 는 겉모습이 똑같습니다.
# 덤프가 없으면 영영 구분할 수 없습니다.
def test_a_course_without_handouts_is_not_a_warning(lesson, monkeypatch, caplog):
    """교안을 안 쓰는 과목이 정상입니다.

    2026-11-13 수집에서 이 경고가 7과목에 떴는데 확인해 보니
    **전부 정상**이었습니다. 정상이 매번 경고로 뜨면 진짜 고장이
    그 사이에 묻힙니다.
    """
    monkeypatch.setattr(lesson, "_download_attachments",
                        lambda d: C.Attachments(candidates=0))
    with caplog.at_level("INFO", logger="gymhall"):
        lesson.crawl_lecture_list(C.Course("x", "영어2_23", "이교수"))
    assert "교안이 올라온 주차가 없습니다" in caplog.text, caplog.text
    assert "이 화면에 있는 것들" not in caplog.text, "정상인데 덤프를 찍었습니다"
    assert not any(r.levelname == "WARNING" for r in caplog.records), caplog.text


def test_already_downloaded_is_not_a_warning(lesson, monkeypatch, caplog):
    """두 번째 sync 입니다. 내용 해시가 같아 saved 에서 빠집니다.

    이걸 구분 안 하면 **멀쩡히 받아 둔 과목이 매 실행 경고**를 냅니다.
    """
    monkeypatch.setattr(lesson, "_download_attachments",
                        lambda d: C.Attachments(candidates=14, already=14))
    with caplog.at_level("INFO", logger="gymhall"):
        lesson.crawl_lecture_list(C.Course("x", "기초웹프로그래밍_05", "이교수"))
    assert "이미 받아 뒀습니다" in caplog.text, caplog.text
    assert "이 화면에 있는 것들" not in caplog.text
    assert not any(r.levelname == "WARNING" for r in caplog.records), caplog.text


def test_candidates_but_nothing_got_is_the_real_problem(lesson, monkeypatch, caplog):
    """누를 것은 있는데 하나도 못 받았습니다. 이때만 경고 + 덤프."""
    monkeypatch.setattr(lesson, "_download_attachments",
                        lambda d: C.Attachments(candidates=14))
    with caplog.at_level("WARNING", logger="gymhall"):
        lesson.crawl_lecture_list(C.Course("x", "생명과학의이해_01", "이교수"))
    assert "누를 것이 14개" in caplog.text, caplog.text
    assert "이 화면에 있는 것들" in caplog.text, (
        "진짜 문제인데 단서를 안 남겼습니다 — 다음 실행에서도 원인을 못 가립니다"
    )
    assert "[프레임" in caplog.text


def test_handouts_found_means_no_diagnostic_noise(lesson, caplog):
    """받았으면 덤프를 찍지 않습니다. 매 과목 쏟아지면 안 봅니다."""
    SERVER_MODE["mode"] = "pdf"
    with caplog.at_level("WARNING", logger="gymhall"):
        lesson.crawl_lecture_list(
            C.Course("2026211605005", "기초웹프로그래밍_05", "이교수"))
    assert "이 화면에 있는 것들" not in caplog.text


def test_failed_handouts_are_reported_as_errors(lesson, monkeypatch):
    """받다가 실패한 것은 '안 올렸다' 와 다릅니다. stats.errors 로 올립니다."""
    att = C.Attachments(failed=["1장.pdf", "2장.pdf"], candidates=14)
    monkeypatch.setattr(lesson, "_download_attachments", lambda d: att)
    before = len(lesson.stats.errors)
    lesson.crawl_lecture_list(C.Course("x", "과목가", "이교수"))
    assert len(lesson.stats.errors) > before
    assert any("강의목록" in e and "2개" in e for e in lesson.stats.errors), \
        lesson.stats.errors


# --- 집계가 실제로 채워지는가 (연결부) ---------------------------------
def test_already_downloaded_files_are_counted(lesson, tmp_path):
    """두 번 받으면 두 번째는 already 로 세야 합니다.

    가짜 Attachments 로는 안 잡힙니다. 진짜 _download_attachments 를
    두 번 돌립니다.
    """
    SERVER_MODE["mode"] = "pdf"
    first = lesson._download_attachments(tmp_path / "a")
    assert len(first.saved) == 14 and first.already == 0
    assert first.candidates == 14

    second = lesson._download_attachments(tmp_path / "b")
    assert second.saved == [], "같은 파일을 또 받았습니다"
    assert second.already == 14, second
    assert second.candidates == 14, "후보 수는 그대로여야 합니다"
    assert not second.nothing_to_get and not second.got_nothing_new


def test_a_page_without_attachments_reports_zero_candidates(browser, server, tmp_path):
    from gymhall.eclass.urls import Site
    from gymhall.paths import Layout
    from gymhall.state import Store

    pg = browser.new_page()
    pg.goto(f"{server}/{EMPTY}")
    try:
        c = C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                      Store.open(tmp_path / "s.json"), term="2026-2", menus={})
        got = c._download_attachments(tmp_path / "att")
        assert got.candidates == 0 and got.nothing_to_get
        assert not got.got_nothing_new, "후보가 0이면 '못 받았다' 가 아닙니다"
    finally:
        pg.close()
