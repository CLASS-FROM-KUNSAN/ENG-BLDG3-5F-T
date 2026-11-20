"""학습현황(menu_00100) 수집 — 통합 과제현황·활동요약 표, 본인 PII 제외.

학습현황은 과목 전체 활동을 한 페이지에 모읍니다. 과제현황 리스트는 모든
과제의 제출여부·점수를 한 줄씩 담은 '요약판' 입니다. 성적·개인정보가 있어
config 에서 기본 꺼짐 + 업로드 제외(SENSITIVE_MENUS)이고, 켜면 로컬에만
저장됩니다. 본인 정보(학번/이메일/전화) 표는 담지 않습니다.
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

FIXTURES = Path(__file__).parent / "fixtures"
PROGRESS = "eclass_progress.html"
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

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(Handler, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


class _Shim:
    def __init__(self, page):
        self.page = page
        self.timeout_ms = 4000


def _crawler(browser, tmp_path, page):
    from syncnote.eclass.urls import Site
    from syncnote.paths import Layout
    from syncnote.state import Store

    return C.Crawler(_Shim(page), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "state.json"), term="2026-2", menus={})


@pytest.fixture
def prog(browser, server, tmp_path):
    pg = browser.new_page()
    pg.goto(f"{server}/{PROGRESS}")
    c = _crawler(browser, tmp_path, pg)
    yield c
    pg.close()


COURSE = C.Course("2026210217623", "영어2_23", "송교수")


def _md(tmp_path):
    return next(tmp_path.rglob("학습현황.md"), None)


def test_progress_writes_status_tables(prog, tmp_path):
    prog.crawl_progress(COURSE)
    md = _md(tmp_path)
    assert md is not None, "학습현황.md 가 없습니다"
    text = md.read_text(encoding="utf-8")
    # 섹션들
    for cap in ("활동 요약", "과제현황 리스트", "토론현황 리스트", "시험현황 리스트"):
        assert f"## {cap}" in text, (cap, text[:300])
    # 통합 과제현황: 모든 과제의 제출여부가 한 표에
    assert "Helping My Community 녹음 파일 제출" in text
    assert "제출" in text and "미제출" in text
    assert "점수(취득점수/만점점수)" in text  # 헤더
    assert md.with_suffix(".png").exists(), "학습현황 스크린샷이 없습니다"


def test_personal_info_table_is_excluded(prog, tmp_path):
    """본인 정보(학번/이름/전화) 표는 담지 않습니다."""
    prog.crawl_progress(COURSE)
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert "학생정보" not in text
    assert "0000000" not in text and "010-0000-0000" not in text
    assert "홍길동" not in text


def test_course_banner_tables_are_not_included(prog, tmp_path):
    """#bodyContent 밖의 과목 배너(이수과목/담당교수) 표는 안 들어갑니다."""
    prog.crawl_progress(COURSE)
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert "이수과목과 학점" not in text and "담당교수 정보" not in text


def test_second_run_is_skipped(prog, tmp_path):
    prog.crawl_progress(COURSE)
    before = prog.stats.posts
    prog.crawl_progress(COURSE)
    assert prog.stats.posts == before, "같은 내용을 또 저장했습니다"
    assert prog.stats.skipped >= 1


def test_relogin_stub_is_skipped_not_saved(browser, tmp_path):
    """과목 세션이 없으면 '다시 로그인' 안내만 옵니다 — 빈 현황으로 저장 안 함."""
    pg = browser.new_page()
    pg.set_content(
        '<div id="bodyContent"><h2>학습 현황</h2>'
        '<p>처리 중 오류가 발생했습니다. 다시 로그인 후 사용해 주십시오.</p></div>')
    c = _crawler(browser, tmp_path, pg)
    try:
        c.crawl_progress(COURSE)
        assert _md(tmp_path) is None, "오류 스텁을 현황으로 저장했습니다"
        assert any("과목 세션이 없어" in e for e in c.stats.errors), c.stats.errors
    finally:
        pg.close()
