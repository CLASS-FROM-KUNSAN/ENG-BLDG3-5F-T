"""계정 레벨 메뉴 수집(crawl_account) + 개인정보 마스킹.

계정에 딸린 9개 메뉴(학습시간표·수강과목·공개강좌·학습도우미 홈/공지/질의응답/
행사안내/FAQ·커뮤니티)를 과목 루프 밖에서 1회 수집합니다. 메뉴·정보는 하나도
빼지 않되(표 있으면 표, 없으면 본문, 빈 메뉴는 '(내용 없음)'), 타인·본인
개인정보(학번/이메일/전화/이름)는 마스킹합니다.
"""

from __future__ import annotations

import http.server
import os
import socket
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from syncnote.eclass import anon


# --- 마스킹 단위 테스트(브라우저 불필요) ------------------------------
def test_mask_pii_masks_the_unambiguous_tokens():
    t = anon.mask_pii("홍길동(9999999) test@test.com 010-1234-5678 사번 1234567 끝")
    assert "9999999" not in t and "1234567" not in t
    assert "test@test.com" not in t and "010-1234-5678" not in t
    assert "<NAME>(<ID>)" in t and "<EMAIL>" in t and "<PHONE>" in t and "<ID>" in t
    assert "끝" in t  # 내용은 보존


def test_mask_pii_keeps_non_pii_content():
    t = anon.mask_pii("1주차 HTML5 과제 제출 2026-11-12 23:59")
    assert t == "1주차 HTML5 과제 제출 2026-11-12 23:59", t  # 날짜/제목 안 건드림


def test_mask_pii_masks_own_name():
    t = anon.mask_pii("담당: 홍길동 교수", own_names=("홍길동",))
    assert "홍길동" not in t and "<NAME>" in t


def test_name_columns_cover_common_headers():
    for h in ("작성자", "이름", "교수명", "담당교수", "제출자"):
        assert h in anon.NAME_COLUMNS


# --- mask_name: 구조상 '사람 이름' 인 칸 전체 가리기 --------------------
def test_mask_name_masks_a_bare_name():
    assert anon.mask_name("홍길동") == "<NAME>"
    assert anon.mask_name("홍길동(교수)") == "<NAME>"  # 사번 없는 역할 표기도


def test_mask_name_masks_name_with_id():
    assert anon.mask_name("김진욱 (101394)") == "<NAME>(<ID>)"


def test_mask_name_keeps_empty_empty():
    assert anon.mask_name("") == "" and anon.mask_name("   ") == ""


def test_mask_name_masks_own_name():
    assert anon.mask_name("홍길동", own_names=("홍길동",)) == "<NAME>"


# --- mask_filename: 첨부 파일명 속 PII, 파일 시스템 안전 문자 ------------
def test_mask_filename_masks_student_id_and_keeps_extension():
    out = anon.mask_filename("20201234_과제제출.zip")
    assert "20201234" not in out and out.endswith(".zip")
    assert "[ID]" in out and "과제제출" in out  # 사번만 가리고 내용은 보존


def test_mask_filename_masks_own_name():
    out = anon.mask_filename("20201234_홍길동.hwp", own_names=("홍길동",))
    assert "홍길동" not in out and "20201234" not in out
    assert out.endswith(".hwp") and "[이름]" in out and "[ID]" in out


def test_mask_filename_uses_filesystem_safe_brackets_not_angle():
    """파일명에 < > 가 들어가면 Windows 에서 파일을 못 만듭니다."""
    out = anon.mask_filename("홍길동(101394)_리포트.pdf")
    assert "<" not in out and ">" not in out
    assert "[이름]([ID])" in out and out.endswith(".pdf")


def test_mask_filename_leaves_clean_names_untouched():
    assert anon.mask_filename("3주차_강의자료.pdf") == "3주차_강의자료.pdf"


# --- crawl_account 전체 흐름(Playwright) ------------------------------
sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]

PROFILE = '<div id="header"><li class="w230"><dl><dt class="fontB">홍길동</dt></dl></li></div>'

# menuId -> #bodyContent 안쪽 HTML. 없는 메뉴는 '빈 내용' 페이지.
PAGES = {
    "menu_00252": PROFILE + """<div id="bodyContent"><h2>학습 시간표</h2>
      <table><caption>시간표</caption>
        <thead><tr><th>요일</th><th>과목</th><th>담당교수</th></tr></thead>
        <tbody><tr><td>월</td><td>영어2</td><td>송지영</td></tr>
               <tr><td>화</td><td>생명과학</td><td>최동수</td></tr></tbody></table></div>""",
    "menu_00237": PROFILE + """<div id="bodyContent"><h2>공지사항</h2>
      <table class="boardListBasic"><caption>공지</caption>
        <thead><tr><th>번호</th><th>제목</th><th>작성자</th><th>작성일</th></tr></thead>
        <tbody>
          <tr><td>1</td><td>장학금 안내</td><td>김직원</td><td>2026-11-01</td></tr>
          <tr><td>2</td><td>도서관 연장 운영</td><td>이사서</td><td>2026-11-03</td></tr>
        </tbody></table>
      <p>문의: helpdesk@test.ac.kr 010-9876-5432</p></div>""",
    "menu_00215": PROFILE + """<div id="bodyContent"><h2>FAQ</h2>
      <table class="boardListBasic"><caption>자주찾는질문</caption>
        <thead><tr><th>번호</th><th>질문</th></tr></thead>
        <tbody><tr><td>1</td><td>비밀번호 재설정은 어떻게 하나요?</td></tr></tbody></table></div>""",
}
EMPTY = PROFILE + '<div id="bodyContent"><h2>메뉴</h2><p>조회된 내용이 없습니다.</p></div>'


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

        def do_GET(self):  # noqa: N802
            q = parse_qs(urlsplit(self.path).query)
            mid = (q.get("mainDTO.menuId") or [""])[0]
            body = ("<!doctype html><meta charset=utf-8>"
                    + PAGES.get(mid, EMPTY)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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


class _Shim:
    def __init__(self, page):
        self.page = page
        self.timeout_ms = 5000

    def goto(self, url, **k):
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)


@pytest.fixture
def crawler(browser, server, tmp_path):
    from syncnote.eclass import crawler as C
    from syncnote.eclass.urls import Site
    from syncnote.paths import Layout
    from syncnote.state import Store

    pg = browser.new_page()
    c = C.Crawler(_Shim(pg), Site(base_url=server), Layout(tmp_path),
                  Store.open(tmp_path / "s.json"), term="2026-2", menus={})
    yield c
    pg.close()


def _md(tmp_path):
    return next(tmp_path.rglob("계정정보.md"), None)


ALL_LABELS = ["학습시간표", "수강과목", "공개강좌", "학습도우미홈", "도우미공지",
              "도우미질의응답", "행사안내", "자주찾는질문", "커뮤니티"]


def test_all_nine_menus_are_collected(crawler, tmp_path):
    crawler.crawl_account()
    md = _md(tmp_path)
    assert md is not None, "계정정보.md 가 없습니다"
    text = md.read_text(encoding="utf-8")
    for label in ALL_LABELS:
        assert f"## {label}" in text, f"빠진 메뉴: {label}"


def test_tables_and_data_are_kept(crawler, tmp_path):
    crawler.crawl_account()
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert "영어2" in text and "생명과학" in text   # 시간표 내용 보존
    assert "장학금 안내" in text                     # 공지 제목 보존
    assert "비밀번호 재설정" in text                 # FAQ 보존


def test_empty_menu_is_noted_not_dropped(crawler, tmp_path):
    crawler.crawl_account()
    text = _md(tmp_path).read_text(encoding="utf-8")
    # 행사안내 등 내용 없는 메뉴도 섹션은 남고 안내문(빈 내용)이 보존됨
    assert "## 행사안내" in text
    assert "조회된 내용이 없습니다" in text


def test_name_columns_are_masked(crawler, tmp_path):
    """작성자/담당교수 열 값은 <NAME> 으로 가립니다."""
    crawler.crawl_account()
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert "김직원" not in text and "이사서" not in text   # 공지 작성자
    assert "송지영" not in text and "최동수" not in text   # 시간표 담당교수
    assert "<NAME>" in text


def test_account_menu_returns_name_column_values_for_screenshot(crawler, server):
    """이름 열 '맨 이름' 을 .md 에선 가리고, 스크린샷용으로 따로 돌려줍니다.

    사번 없는 맨 이름은 정규식으론 못 잡으니, 이 값을 .png 마스킹의
    known_names 로 넘겨야 그림에도 안 남습니다(crawl_account 가 그렇게 씀).
    """
    crawler.br.goto(server + "/x?mainDTO.menuId=menu_00252")  # 학습 시간표
    md_text, names = crawler._account_menu_markdown("학습시간표")
    assert "송지영" in names and "최동수" in names   # 스크린샷용 반환
    assert "송지영" not in md_text and "<NAME>" in md_text   # .md 에선 가려짐


def test_known_name_registry_collects_and_masks_free_text(crawler):
    """구조적으로 알게 된 이름을 모아 자유 본문의 '맨 이름' 까지 가립니다."""
    crawler._note_names(["김진욱 (101394)", "이영희", "x", "1234567", ""])
    names = set(crawler._mask_names())
    assert "김진욱" in names   # '이름(사번)' 에서 이름만 떼어 담음
    assert "이영희" in names
    assert "x" not in names and "1234567" not in names   # 2자 미만·숫자 제외
    masked = anon.mask_pii("담당 이영희, 김진욱 드림", own_names=tuple(names))
    assert "이영희" not in masked and "김진욱" not in masked and "<NAME>" in masked


def test_pii_tokens_are_masked(crawler, tmp_path):
    crawler.crawl_account()
    text = _md(tmp_path).read_text(encoding="utf-8")
    assert "helpdesk@test.ac.kr" not in text and "<EMAIL>" in text
    assert "010-9876-5432" not in text and "<PHONE>" in text
    assert "홍길동" not in text   # 본인 이름(프로필에서 추출) 마스킹


def test_second_run_is_skipped(crawler, tmp_path):
    crawler.crawl_account()
    before = crawler.stats.posts
    crawler.crawl_account()
    assert crawler.stats.posts == before
    assert crawler.stats.skipped >= 1
