"""실제 '학습 목차' 페이지(익명화본)로 수집기 파싱을 검증합니다.

tests/fixtures/eclass_study_home.html 은 2026-11 에 받은 실제 eClass
강의실 홈에서 사람 이름·학번·이메일만 가명으로 바꾼 것입니다. 구조·
JS·메뉴 주소는 손대지 않았습니다. 따라서 여기 테스트가 깨지면 둘 중
하나입니다 — 우리 선택자가 틀렸거나, 학교가 LMS 를 바꿨거나.

정규식으로 보는 test_eclass_html.py 와 달리 진짜 Chromium 으로 띄워서
DOM 과 JS 가 있는 상태를 봅니다.
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

from syncnote.eclass.crawler import Crawler  # noqa: E402
from syncnote.eclass.urls import Site, parse_course_option  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
STUDY_HOME = FIXTURES / "eclass_study_home.html"
COURSE_ID = "2026210217623"
CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]


# --- 브라우저 없이 되는 것 -------------------------------------------
def _html() -> str:
    return STUDY_HOME.read_text(encoding="utf-8")


def _left_menu_links() -> list[tuple[str, str]]:
    """좌측 메뉴의 (링크 텍스트, href) 쌍. 중복은 그대로 둡니다."""
    block = re.search(r'<ul id="leftMenu".*?(?=<!--|\Z)', _html(), re.S)
    assert block, "leftMenu 블록이 없습니다"
    return re.findall(r'<a href="([^"]+)"[^>]*>\s*([^<]+?)\s*</a>', block.group(0))


def _ids(url: str) -> tuple[str | None, str | None]:
    q = parse_qs(urlsplit(url).query)
    return (q.get("mainDTO.parentMenuId", [None])[0], q.get("mainDTO.menuId", [None])[0])


# 좌측 메뉴 표기 -> 우리 내부 키
MENU_KEY_BY_LABEL = {
    "학습 목차": "curriculum",
    "강의 목록": "lecture_list",
    "공지사항": "notice",
    "질의응답": "qna",
    "강의자료실": "materials",
    "출석": "attendance",
    "과제": "assignment",
    "토론": "discussion",
    "시험": "exam",
    "설문": "survey",
    "팀활동": "team",
    "학습 현황": "progress",
    "수업 계획서": "syllabus",
    "과목 정보": "course_info",
    "성적보기": "grade",
}


def test_fixture_left_menu_has_all_fifteen_targets():
    """사용자가 요청한 15개 메뉴가 실제 페이지에 전부 있는지부터 확인합니다."""
    labels = {label for _, label in _left_menu_links()}
    missing = set(MENU_KEY_BY_LABEL) - labels
    assert not missing, f"픽스처에 없는 메뉴: {missing}"


@pytest.mark.parametrize("label,key", sorted(MENU_KEY_BY_LABEL.items()))
def test_menu_url_matches_the_real_link(label: str, key: str):
    """urls.py 의 menuId 가 실제 페이지의 링크와 같아야 합니다.

    이 테스트가 이 파일에서 가장 중요합니다. menuId 는 외워서 적은 값이
    아니라 실제 페이지에서 뽑은 값이어야 하고, 하나라도 어긋나면 그
    메뉴만 조용히 다른 화면을 긁습니다.
    """
    real = [href for href, text in _left_menu_links() if text == label]
    assert real, f"'{label}' 링크가 픽스처에 없습니다"
    want_parent, want_menu = _ids(real[0])

    built = Site().menu_url(COURSE_ID, key)
    assert built is not None, f"{key}: menu_url 이 None 입니다(클릭 경로로 빠집니다)"
    got_parent, got_menu = _ids(built)
    assert (got_parent, got_menu) == (want_parent, want_menu), (
        f"{label}: 실제 {want_parent}/{want_menu} != 우리 {got_parent}/{got_menu}"
    )


def test_board_urls_carry_the_board_info_id_suffix():
    """게시판을 실제로 고르는 것은 boardInfoId 접미사입니다."""
    for key, suffix in (("notice", "N"), ("qna", "Q"), ("materials", "P")):
        q = parse_qs(urlsplit(Site().menu_url(COURSE_ID, key)).query)
        assert q["boardInfoDTO.boardInfoId"] == [f"{COURSE_ID}-{suffix}"]
        assert q["boardInfoDTO.boardType"] == ["course"]


def test_course_options_include_non_numeric_ids_and_bnormal():
    """과목 select 에는 CORS_ 로 시작하는 id 와 bnormal 유형이 섞여 있습니다."""
    opts = re.findall(r"<option value = '([^']*)'[^>]*>\s*([^<]+?)\s*</option>", _html())
    parsed = [parse_course_option(v) for v, _ in opts]
    parsed = [p for p in parsed if p]
    assert len(parsed) >= 9, parsed
    assert any(cid.startswith("CORS_") for cid, _, _ in parsed)
    assert {t for _, _, t in parsed} >= {"normal", "bnormal"}


def test_open_type_routing_covers_mooc():
    """viewCourse() 는 open 과 mooc 를 똑같이 /Lesson.do 로 보냅니다."""
    js = _html()
    assert 'openType!=="open"&&openType!=="mooc"' in js.replace(" ", "")
    assert frozenset({"open", "mooc"}) == Crawler.LESSON_HOME_TYPES


# --- 브라우저가 필요한 것 ---------------------------------------------
@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    class Handler(http.server.SimpleHTTPRequestHandler):
        # 실제 eClass 는 charset=UTF-8 을 헤더로 보냅니다. 페이지 안에는
        # <meta charset> 이 없어서, 헤더가 빠지면 크로미움이 windows-1252
        # 로 읽어 한글이 전부 깨집니다. 서버를 사이트와 같게 맞춥니다.
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
    """Crawler 가 실제로 쓰는 것은 .page 와 .timeout_ms 뿐입니다."""

    def __init__(self, page):
        self.page = page
        self.timeout_ms = 5000


@pytest.fixture
def crawler(browser, server, tmp_path):
    from syncnote.paths import Layout
    from syncnote.state import Store

    pg = browser.new_page()
    pg.goto(f"{server}/{STUDY_HOME.name}")
    c = Crawler(
        _Shim(pg), Site(), Layout(tmp_path), Store.open(tmp_path / "state.json"),
        term="2026-2", menus={},
    )
    yield c
    pg.close()


def test_curriculum_window_reads_three_weeks(crawler):
    """학습 목차는 한 번에 세 주차만 보여 줍니다. 그대로 저장하면 3주뿐입니다."""
    win = crawler._curriculum_window()
    assert win is not None, "학습목차 표를 찾지 못했습니다"
    assert len(win["weeks"]) == 3, win["weeks"]
    assert all("주차" in w for w in win["weeks"]), win["weeks"]
    labels = {r["label"] for r in win["rows"]}
    assert {"강의영상", "과 제"} <= labels, labels


def test_curriculum_rows_align_with_week_columns(crawler):
    """행마다 셀 개수가 주차 수와 같아야 주차별로 쪼갤 수 있습니다."""
    win = crawler._curriculum_window()
    n = len(win["weeks"])
    for row in win["rows"]:
        assert len(row["cells"]) == n, f"{row['label']}: {len(row['cells'])} != {n}"


def test_week_arrows_are_reachable(crawler):
    """좌우 화살표 선택자가 실제 DOM 에서 잡혀야 주차를 넘길 수 있습니다."""
    page = crawler.br.page
    assert page.locator(Crawler.CURRICULUM_PREV).count() >= 1, "이전 주차 화살표 못 찾음"
    assert page.locator(Crawler.CURRICULUM_NEXT).count() >= 1, "다음 주차 화살표 못 찾음"
    href = page.locator(Crawler.CURRICULUM_NEXT).first.get_attribute("href")
    assert "viewNextPage" in href, href


def test_step_week_reports_failure_when_nothing_changes(crawler):
    """화살표를 눌러도 화면이 그대로면 실패로 봐야 합니다.

    여기 픽스처는 POST 에 응답할 서버가 없어 창이 바뀌지 않습니다.
    그때 True 를 돌려주면 collect_curriculum 이 같은 세 주차를
    30번 반복해서 모으고, 사용자는 전부 받은 줄 압니다.
    """
    assert crawler._step_week(Crawler.CURRICULUM_NEXT) is False


def test_collect_curriculum_terminates_without_a_live_server(crawler):
    """서버가 없어도 무한 반복에 빠지지 않고 보이는 주차만 돌려줍니다."""
    weeks = crawler.collect_curriculum(
        type("C", (), {"name": "테스트", "course_id": COURSE_ID})()
    )
    assert len(weeks) == 3, list(weeks)
    assert not crawler.stats.errors, crawler.stats.errors


# --- 주차를 못 넘기면 알려야 합니다 ------------------------------------
# 2026-11-12 실행: 16주 과목 아홉 개가 전부 '3주차' 로 저장됐습니다.
# 첫 창만 읽고 한 칸도 못 넘겼는데 아무 경고가 없었습니다.
def test_failing_to_step_is_warned_not_silent(crawler, caplog):
    import logging

    course = type("C", (), {"name": "과목가", "course_id": "C1"})()
    with caplog.at_level(logging.WARNING):
        weeks = crawler.collect_curriculum(course)
    assert len(weeks) == 3
    assert "한 칸도 못 넘겼습니다" in caplog.text
    # 화살표가 있는지 없는지를 로그가 구분해 줘야 다음에 어디를 볼지 압니다.
    assert "prev=" in caplog.text and "next=" in caplog.text


def test_step_week_does_not_raise_when_nothing_moves(crawler):
    """못 넘기면 False 일 뿐, 예외로 올라가면 과목 전체가 날아갑니다.

    (왜 멈췄는지는 debug 로그에 남지만, syncnote 로거는 propagate 를
     꺼 두어서 caplog 로 보이지 않습니다. 여기서는 계약만 봅니다.)
    """
    assert crawler._step_week(Crawler.CURRICULUM_NEXT) is False
    assert crawler._step_week(Crawler.CURRICULUM_PREV) is False
