"""쪽지함 수집.

강의자료가 쪽지로도 옵니다("[과목사] 한문단쓰기 자료 탑재 완료 알림").
보낸이 칸은 `김진욱 (101394)` 처럼 이름과 사번이 함께 들어 있어
익명화해서 저장하고, 본문은 그대로 둡니다.

fixtures/eclass_message_list.html 은 실제 받은 쪽지함에서 이름·사번·
과목명만 바꾼 것입니다. 구조와 JS 는 손대지 않았습니다.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from gymhall.eclass import anon  # noqa: E402
from gymhall.eclass import crawler as C  # noqa: E402
from gymhall.eclass.urls import Site  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
MESSAGES = "eclass_message_list.html"
CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]


# --- 익명화 -----------------------------------------------------------
def test_sender_name_and_id_are_both_removed():
    out = anon.sender("김진욱 (101394)")
    assert "김진욱" not in out
    assert "101394" not in out
    assert out.startswith("보낸이-")


def test_same_sender_gets_the_same_label():
    """'이 셋은 같은 사람이 보냈다' 를 알 수 있어야 합니다."""
    a = anon.sender("김진욱 (101394)")
    assert anon.sender("김진욱(101394)") == a
    assert anon.sender("  김진욱 (101394) ") == a


def test_different_senders_get_different_labels():
    assert anon.sender("김진욱 (101394)") != anon.sender("송지영 (504525)")
    # 이름이 같아도 사번이 다르면 다른 사람입니다.
    assert anon.sender("김진욱 (101394)") != anon.sender("김진욱 (101395)")


def test_a_sender_without_parens_is_still_hidden():
    out = anon.sender("관리자")
    assert out.startswith("보낸이-") and "관리자" not in out


def test_an_empty_sender_stays_empty():
    assert anon.sender("") == ""
    assert anon.sender("   ") == ""


# --- 주소 -------------------------------------------------------------
def test_message_box_urls():
    s = Site()
    q = parse_qs(urlsplit(s.messages()).query)
    assert q["cmd"] == ["viewMessageList"] and q["messageType"] == ["RECEIVE"]
    assert "curPage" not in parse_qs(urlsplit(s.messages("RECEIVE", 1)).query)
    assert parse_qs(urlsplit(s.messages("SEND", 3)).query)["curPage"] == ["3"]


def test_unknown_message_box_is_rejected():
    with pytest.raises(ValueError):
        Site().messages("INBOX")


def test_message_pagination_is_a_plain_get():
    """게시판과 달리 쪽지 페이지 이동은 평범한 GET 입니다.

    게시판처럼 클릭으로 넘기려 들면 쓸데없이 깨집니다.
    """
    html = (FIXTURES / MESSAGES).read_text(encoding="utf-8")
    links = re.findall(r'href="(/Message\.do\?cmd=viewMessageList[^"]*)"', html)
    assert links, "쪽지함 링크가 없습니다"
    assert any("messageType=SEND" in a for a in links)
    assert any("messageType=WASTEBASKET" in a for a in links)


def test_move_message_view_posts_two_ids():
    html = (FIXTURES / MESSAGES).read_text(encoding="utf-8")
    body = re.search(r"function moveMessageView\(.*?\n\}", html, re.S).group(0)
    assert 'f["cmd"].value = "viewMessageContents"' in body
    assert "messageBoxDTO.messageId" in body
    assert "messageBoxDTO.messageSendId" in body


def test_message_attachment_goes_through_dwr():
    """첨부는 DWR 로 zip 을 만든 뒤 콜백에서 fileDownload 를 부릅니다.

    onclick 에 주소가 없으므로 이것만은 눌러서 받아야 합니다.
    """
    html = (FIXTURES / MESSAGES).read_text(encoding="utf-8")
    assert "MessageWork.compressDownloadAttachFile" in html
    body = re.search(r"function compressDownloadCallback\(.*?\n\}", html, re.S).group(0)
    assert "fileDownload(" in body


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

    def goto(self, url):
        self.page.goto(url)


@pytest.fixture
def crawler(browser, server, tmp_path):
    from gymhall.paths import Layout
    from gymhall.state import Store

    pg = browser.new_page()
    pg.goto(f"{server}/{MESSAGES}")
    c = C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                  Store.open(tmp_path / "state.json"), term="2026-2", menus={})
    yield c
    pg.close()


def test_message_rows_are_read_with_the_right_columns(crawler):
    """칸 순서가 게시판과 다릅니다 — 맨 앞이 체크박스입니다.

    게시판용 read_board_rows 로 읽으면 번호 칸이 비고 보낸이 자리에
    '파일' 칸이 들어옵니다.
    """
    rows = crawler.read_message_rows()
    assert len(rows) == 10, len(rows)
    first = rows[0]
    assert first["no"] == "28", first
    assert first["title"] == "과목다 수업 공지", first
    assert first["date"] == "2026-11-10", first
    assert "(" in first["sender"], first["sender"]


def test_board_reader_would_misread_the_message_table(crawler):
    """같은 table.boardListBasic 이지만 칸 뜻이 다릅니다."""
    rows = crawler.read_board_rows()
    assert rows, "게시판 리더가 행은 잡습니다"
    assert rows[0]["no"] != "28", "번호 칸이 체크박스 칸과 어긋나야 정상"


def test_sender_is_anonymised_before_it_is_written(crawler, tmp_path):
    rows = crawler.read_message_rows()
    raw = rows[0]["sender"]
    label = anon.sender(raw)
    name = raw.split("(")[0].strip()
    assert name and name not in label


# --- 못 읽은 것과 0건을 구분 -------------------------------------------
# 2026-11-12 10:34 러너 기록:
#   메뉴 처리 실패: 기초웹프로그래밍_05/강의목록: object of type 'NoneType' has no len()
#   게시판 목록 읽기 실패: Page.evaluate: TypeError: refs.set is not a function
# 앞엣것은 evaluate 가 None 을 돌려줬는데 그대로 len() 에 넘긴 것이고,
# 뒤엣것은 그 실패를 경고로만 남기고 빈 목록을 돌려줘서 게시판 27곳이
# 전부 '글이 없습니다' 로 끝난 것입니다. 둘 다 조용히 0건이 됩니다.
class _BadPage:
    """evaluate 가 엉뚱한 값을 내거나 터지는 페이지."""

    def __init__(self, result=None, boom=False):
        self.url = "http://example.invalid/"
        self._result = result
        self._boom = boom

    def evaluate(self, *a, **k):
        if self._boom:
            raise RuntimeError("Page.evaluate: TypeError: refs.set is not a function")
        return self._result


def _crawler_with(page, tmp_path):
    from gymhall.paths import Layout
    from gymhall.state import Store

    return C.Crawler(_Shim(page), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "s.json"), term="2026-2", menus={})


@pytest.mark.parametrize("reader", ["read_board_rows", "read_lesson_weeks",
                                    "read_message_rows"])
def test_none_from_evaluate_raises_instead_of_crashing_later(reader, tmp_path):
    """None 을 그대로 돌려주면 호출부가 len(None) 으로 터집니다."""
    c = _crawler_with(_BadPage(result=None), tmp_path)
    with pytest.raises(C.PageReadError):
        getattr(c, reader)()


@pytest.mark.parametrize("reader", ["read_board_rows", "read_lesson_weeks",
                                    "read_message_rows"])
def test_evaluate_failure_is_not_reported_as_zero_rows(reader, tmp_path):
    c = _crawler_with(_BadPage(boom=True), tmp_path)
    with pytest.raises(C.PageReadError):
        getattr(c, reader)()


def test_board_read_failure_becomes_a_recorded_error(tmp_path):
    """crawl_board 가 '글이 없습니다' 로 넘기면 안 됩니다."""
    c = _crawler_with(_BadPage(boom=True), tmp_path)
    c.board_page_count = lambda: 1
    course = C.Course("C1", "과목가", "이교수")
    c.crawl_board(course, "notice")
    assert c.stats.errors, "조용히 넘어갔습니다"
    assert c.stats.boards_empty == 0, "못 읽은 것을 '0건' 으로 셌습니다"


def test_page_signature_tolerates_a_read_failure(tmp_path):
    """지문은 못 읽어도 예외를 올리지 않습니다(이동 실패로 처리)."""
    c = _crawler_with(_BadPage(boom=True), tmp_path)
    assert c._page_signature() == ""


# --- 첨부 실패와 '완료' 를 섞지 않기 -----------------------------------
# 리뷰 지적: 첨부를 못 받아도 게시글을 mark_downloaded 로 기록하면,
# 다음 실행이 그 글을 건너뛰어 실패한 첨부를 영영 다시 받지 않습니다.
from gymhall.eclass.crawler import Attachments  # noqa: E402


def _c(tmp_path):
    from gymhall.paths import Layout
    from gymhall.state import Store

    return C.Crawler(_Shim(_BadPage()), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "s.json"), term="2026-2", menus={})


def test_a_failed_attachment_leaves_the_post_unfinished(tmp_path):
    c = _c(tmp_path)
    md = tmp_path / "글.md"
    md.write_text("본문", encoding="utf-8")
    att = Attachments(saved=[], failed=["자료.pdf"])
    c._finish_post("KEY", md, att, expected_file=True, what="과목가/공지/글", extra={})
    assert not c.store.is_downloaded("KEY"), "실패했는데 완료로 적었습니다"
    assert c.store.get_meta("pending:KEY") == str(md)
    assert c.stats.errors


def test_a_post_with_every_attachment_saved_is_finished(tmp_path):
    c = _c(tmp_path)
    md = tmp_path / "글.md"
    md.write_text("본문", encoding="utf-8")
    att = Attachments(saved=[tmp_path / "자료.pdf"], failed=[])
    c._finish_post("KEY", md, att, expected_file=True, what="과목가/공지/글", extra={})
    assert c.store.is_downloaded("KEY")
    assert not c.stats.errors


def test_a_post_with_no_attachment_at_all_is_finished(tmp_path):
    """첨부 표시가 없는 글은 받을 것이 없어도 완료입니다."""
    c = _c(tmp_path)
    md = tmp_path / "글.md"
    md.write_text("본문", encoding="utf-8")
    c._finish_post("KEY", md, Attachments(), expected_file=False,
                   what="과목가/공지/글", extra={})
    assert c.store.is_downloaded("KEY")
    assert not c.stats.errors


def test_a_post_marked_as_having_a_file_but_none_found_is_unfinished(tmp_path):
    """목록에 첨부 표시가 있는데 링크를 못 찾은 경우도 실패입니다.

    선택자가 어긋나면 '첨부 0개' 와 구분되지 않습니다.
    """
    c = _c(tmp_path)
    md = tmp_path / "글.md"
    md.write_text("본문", encoding="utf-8")
    c._finish_post("KEY", md, Attachments(), expected_file=True,
                   what="과목가/공지/글", extra={})
    assert not c.store.is_downloaded("KEY")
    assert c.stats.errors


def test_retry_reuses_the_same_file_instead_of_making_a_copy(tmp_path):
    """미완료 글을 다시 받을 때 '제목_2.md' 를 만들지 않습니다."""
    c = _c(tmp_path)
    out = tmp_path / "공지"
    out.mkdir()
    first = c._pending_path("KEY", out, "1_글")
    first.write_text("x", encoding="utf-8")
    c._finish_post("KEY", first, Attachments(saved=[], failed=["a.pdf"]),
                   expected_file=True, what="w", extra={})
    second = c._pending_path("KEY", out, "1_글")
    assert second == first, (first, second)


def test_finishing_clears_the_pending_marker(tmp_path):
    c = _c(tmp_path)
    md = tmp_path / "글.md"
    md.write_text("x", encoding="utf-8")
    c._finish_post("KEY", md, Attachments(saved=[], failed=["a"]),
                   expected_file=True, what="w", extra={})
    assert c.store.get_meta("pending:KEY")
    c._finish_post("KEY", md, Attachments(saved=[md], failed=[]),
                   expected_file=True, what="w", extra={})
    assert c.store.get_meta("pending:KEY") is None
    assert c.store.is_downloaded("KEY")


# --- evaluate 에 인자를 넘기지 않기 -----------------------------------
# 2026-11-12 러너 기록:
#   인자 없음   + 복합 반환 -> 정상 (list_courses 가 9과목을 찾음)
#   문자열 인자 + 복합 반환 -> 조용히 None (read_lesson_weeks)
#   리스트/객체 인자        -> TypeError: refs.set is not a function
# 터지는 쪽은 인자입니다. playwright 의 역직렬화기가 인자를 풀면서
# refs.set 을 부르는데 그 refs 가 Map 이 아닙니다.
import inspect  # noqa: E402
import re as _re  # noqa: E402


def _crawler_source() -> str:
    return inspect.getsource(C)


def test_no_evaluate_call_passes_a_runtime_argument():
    """page.evaluate(expr, arg) 가 하나라도 있으면 러너에서 깨집니다."""
    src = _crawler_source()
    bad = []
    for m in _re.finditer(r"\.evaluate\(", src):
        # 호출 괄호를 균형 맞춰 읽습니다.
        i, depth = m.end() - 1, 0
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[m.end():i]
        # 문자열 리터럴을 지운 뒤 최상위 쉼표가 남으면 인자가 있는 것입니다.
        stripped = _re.sub(r'""".*?"""', "S", call, flags=_re.S)
        stripped = _re.sub(r'"[^"]*"|\'[^\']*\'', "S", stripped)
        if "," in stripped.replace("S", "").strip(" \n)"):
            bad.append(call[:60])
    assert not bad, f"evaluate 에 인자를 넘기는 곳: {bad}"


def test_values_are_baked_into_the_js_source():
    """선택자는 인자가 아니라 JS 소스에 리터럴로 들어갑니다."""
    assert "__SEL__" in _crawler_source()
    assert "_js_literal" in _crawler_source()


def test_eval_json_refuses_an_unfilled_placeholder(tmp_path):
    """자리를 안 채우고 보내면 페이지에 가기 전에 막습니다."""
    class P:
        url = "x"

        def evaluate(self, *a, **k):
            raise AssertionError("페이지까지 가면 안 됩니다")

    with pytest.raises(C.PageReadError, match="채우지 못한"):
        C._eval_json(P(), "() => __SEL__")


def test_eval_json_refuses_a_typo_in_the_placeholder_name(tmp_path):
    class P:
        url = "x"

        def evaluate(self, *a, **k):
            raise AssertionError("페이지까지 가면 안 됩니다")

    with pytest.raises(C.PageReadError, match="자리가 없습니다"):
        C._eval_json(P(), "() => 1", nope="x")


def test_eval_json_parses_the_json_string(tmp_path):
    class P:
        url = "x"

        def evaluate(self, expression):
            assert "\"td a\"" in expression, expression
            return '[{"a": 1}]'

    assert C._eval_json(P(), "() => __SEL__", sel="td a") == [{"a": 1}]


def test_eval_json_rejects_a_non_string_result(tmp_path):
    """복합 반환은 러너에서 조용히 None 이 됩니다. 문자열만 받습니다."""
    class P:
        url = "x"

        def evaluate(self, expression):
            return [{"a": 1}]

    with pytest.raises(C.PageReadError, match="문자열"):
        C._eval_json(P(), "() => 1")
