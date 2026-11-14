"""텍스트 추출본(.md) 옆의 화면 스크린샷(.png).

지키는 것은 셋입니다.

1. `.md` 를 쓰는 자리마다 같은 이름의 `.png` 가 생긴다(연결).
2. `.md` 에 없는 개인정보를 그림으로 새로 내보내지 않는다.
   - 화면 머리의 본인 '학번(이름)님' 은 가린다(픽셀로 확인).
   - 쪽지 보낸이는 `.md` 와 같은 이름표로 바꾼 뒤 찍고, 못 바꾸면
     찍지 않는다(실패 시 닫힘).
3. 스크린샷이 실패해도 글 수집은 성공한다.

로그 문구가 아니라 부작용(파일, 화면 글자, 픽셀)을 봅니다 —
docs/TESTING.md 원칙 5.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

from gymhall.eclass import anon  # noqa: E402
from gymhall.eclass import crawler as C  # noqa: E402
from gymhall.eclass import snapshot as S  # noqa: E402

CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

RAW_SENDER = "김진욱 (101394)"
OTHER_SENDER = "박영희 (202020)"

HEADER = """<div class="login"><form id="loginForm">
  <span class="info" style="display:inline-block;width:240px;height:40px;
        background:#000;color:#fff;font-size:20px">0000000(테스트)님</span>
</form></div>"""

PAGE = f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#fff">{HEADER}
<div id="bodyContent"><h3>강의계획서</h3><p>1주차: 오리엔테이션</p></div>
</body></html>"""

# 쪽지 상세 흉내. 보낸이가 '이름 (사번)', 이름만, 그리고 아래 목록에
# 다른 사람까지 — 실제 화면에서 나올 수 있는 세 꼴을 다 넣습니다.
MESSAGE = f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body>{HEADER}
<table class="boardListBasic"><tbody><tr>
  <td></td><td>1</td><td>{RAW_SENDER}</td>
  <td class="textLeft"><a href="#" onclick="return false;">자료 탑재 알림</a></td>
  <td></td><td>2026-11-13</td>
</tr></tbody></table>
<div class="boardViewBox"><dl><dd class="info">보낸이 {RAW_SENDER}</dd>
  <dd class="text"><p>김진욱 드림. 자료를 올렸습니다.</p></dd></dl></div>
<table><tr><td>{OTHER_SENDER}</td><td>다른 쪽지</td></tr></table>
</body></html>"""


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
        self.timeout_ms = 800

    def login(self, *a, **kw):
        pass


def _crawler(browser, tmp_path, html, **kw):
    from gymhall.eclass.urls import Site
    from gymhall.paths import Layout
    from gymhall.state import Store

    pg = browser.new_page()
    pg.set_content(html)
    return C.Crawler(_Shim(pg), Site(), Layout(tmp_path),
                     Store.open(tmp_path / "state.json"), term="2026-2",
                     menus={}, **kw)


COURSE = C.Course("2026211605005", "과목가", "이교수")


# --- 1. 연결: .md 옆에 .png ------------------------------------------
def test_save_page_text_leaves_a_png_next_to_the_md(browser, tmp_path):
    c = _crawler(browser, tmp_path, PAGE)
    md = c.save_page_text(COURSE, "syllabus")
    assert md is not None and md.exists()
    png = md.with_suffix(".png")
    assert png.exists(), "스크린샷이 없습니다"
    assert png.read_bytes().startswith(PNG_MAGIC)
    assert c.stats.shots == 1 and c.stats.shots_failed == 0
    assert "스크린샷 1" in c.stats.line()


def test_unchanged_page_takes_no_new_screenshot(browser, tmp_path):
    """.md 를 건너뛴 실행에서 그림만 새로 찍으면 둘이 다른 시점이 됩니다."""
    c = _crawler(browser, tmp_path, PAGE)
    first = c.save_page_text(COURSE, "syllabus")
    assert c.save_page_text(COURSE, "syllabus") is None
    assert c.stats.shots == 1
    assert first.with_suffix(".png").exists()


def test_screenshots_can_be_turned_off(browser, tmp_path):
    c = _crawler(browser, tmp_path, PAGE, screenshots=False)
    md = c.save_page_text(COURSE, "syllabus")
    assert md is not None and not md.with_suffix(".png").exists()
    assert c.stats.shots == 0


def test_a_failing_screenshot_does_not_fail_the_page(browser, tmp_path, monkeypatch):
    """그림은 덤입니다. 못 찍었다고 글을 잃으면 안 됩니다."""
    c = _crawler(browser, tmp_path, PAGE)

    def boom(*a, **k):
        raise RuntimeError("디스크 가득 참")

    monkeypatch.setattr(c.br.page, "screenshot", boom)
    md = c.save_page_text(COURSE, "syllabus")
    assert md is not None and md.exists(), "스크린샷 실패로 글까지 잃었습니다"
    assert c.store.is_downloaded(f"page:{COURSE.course_id}:syllabus")
    assert c.stats.shots == 0 and c.stats.shots_failed == 1
    assert "실패 1" in c.stats.line()


# --- 1b. "모든 페이지" 를 구조로 고정 -----------------------------------
# 동작 테스트는 픽스처가 있는 화면만 됩니다. 학습목차(세 주차씩 넘기는
# 화면)와 위험 게시판 목록은 픽스처가 없어서, 거기서 _snapshot 줄을
# 지워도 동작 테스트는 초록이었습니다(변이 P9, P13).
#
# 그래서 규칙을 소스에서 봅니다: Crawler 안에서 `.md` 를 쓰는(write_text)
# 함수는 **같은 경로 변수로** self._snapshot 을 불러야 합니다. 새 화면을
# 추가하면서 스크린샷을 빠뜨려도 여기서 빨개집니다.
def _md_writes_without_snapshot() -> list[str]:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(C))
    crawler = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "Crawler")
    problems: list[str] = []
    for fn in crawler.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        written: list[ast.expr] = []
        shot: set[str] = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "write_text":
                written.append(node.func.value)
            elif (node.func.attr == "_snapshot" and node.args
                  and isinstance(node.args[0], ast.Name)):
                shot.add(node.args[0].id)
        for target in written:
            if not isinstance(target, ast.Name):
                problems.append(f"{fn.name}: 경로를 변수에 담지 않고 바로 씁니다 "
                                f"({ast.unparse(target)}) — 변수로 받아 _snapshot 하세요")
            elif target.id not in shot:
                problems.append(f"{fn.name}: '{target.id}' 를 쓰고 스크린샷을 안 남깁니다")
    return problems


def test_every_md_the_crawler_writes_gets_a_screenshot():
    assert _md_writes_without_snapshot() == []


def test_the_structure_check_actually_finds_a_missing_screenshot(monkeypatch):
    """검사기가 조용히 빈 결과만 돌려주면 위 테스트는 영원히 초록입니다."""
    import inspect

    src = inspect.getsource(C)
    broken = src.replace("            self._snapshot(listing)\n", "", 1)
    assert broken != src, "이 검사의 전제(목록 화면 스크린샷 줄)가 바뀌었습니다"
    real = inspect.getsource
    monkeypatch.setattr(inspect, "getsource",
                        lambda obj: broken if obj is C else real(obj))
    problems = _md_writes_without_snapshot()
    assert any("listing" in p for p in problems), problems


# --- 2a. 본인 정보는 가린다 ---------------------------------------------
def _pixel(browser, png: bytes, x: int, y: int) -> tuple[int, int, int]:
    """PNG 의 (x, y) 픽셀. Pillow 없이 브라우저 캔버스로 읽습니다."""
    pg = browser.new_page()
    try:
        data = base64.b64encode(png).decode()
        pg.set_content(f'<img id="i" src="data:image/png;base64,{data}">')
        pg.wait_for_function("document.getElementById('i').complete")
        return tuple(pg.evaluate(
            """([x, y]) => {
                 const img = document.getElementById('i');
                 const cv = document.createElement('canvas');
                 cv.width = img.naturalWidth; cv.height = img.naturalHeight;
                 const ctx = cv.getContext('2d');
                 ctx.drawImage(img, 0, 0);
                 return Array.from(ctx.getImageData(x, y, 1, 1).data.slice(0, 3));
               }""", [x, y]))
    finally:
        pg.close()


def test_own_student_id_is_masked_in_the_picture(browser, tmp_path):
    """.md 에는 안 들어가는 '학번(이름)님' 을 그림으로 새로 내보내면 안 됩니다.

    그 칸은 검정 바탕이라, 가리지 않으면 가운데 픽셀이 검정입니다.
    """
    c = _crawler(browser, tmp_path, PAGE)
    box = c.br.page.locator("div.login .info").bounding_box()
    md = c.save_page_text(COURSE, "syllabus")
    png = md.with_suffix(".png").read_bytes()
    cx, cy = int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    r, g, b = _pixel(browser, png, cx, cy)
    assert (r, g, b) != (0, 0, 0), "본인 학번 칸이 그대로 찍혔습니다"
    # Playwright 의 기본 마스크 색(#FF00FF)
    assert r > 200 and g < 60 and b > 200, (r, g, b)


# --- 2b. 쪽지 보낸이는 .md 와 같은 이름표로 -----------------------------
def test_sender_pairs_cover_whole_name_and_id_longest_first():
    pairs = S.sender_pairs([RAW_SENDER])
    keys = [a for a, _ in pairs]
    assert keys[0] == RAW_SENDER, "통째 치환이 먼저여야 부분 치환이 안 생깁니다"
    assert "김진욱" in keys and "101394" in keys
    assert {b for _, b in pairs} == {anon.sender(RAW_SENDER)}


def test_message_picture_uses_the_same_label_as_the_md(browser, tmp_path, monkeypatch):
    """_open_message 를 통째로 돌립니다(연결까지 검사)."""
    c = _crawler(browser, tmp_path, MESSAGE)
    monkeypatch.setattr(c, "_download_attachments", lambda d: C.Attachments())
    row = {"index": 0, "no": "1", "title": "자료 탑재 알림", "date": "2026-11-13",
           "has_file": False, "sender": RAW_SENDER}
    label = anon.sender(RAW_SENDER)
    out = tmp_path / "msg"
    out.mkdir()
    c._open_message(row, label, out, "key-msg", "RECEIVE")

    md = next(out.glob("*.md"))
    assert label in md.read_text(encoding="utf-8")
    assert md.with_suffix(".png").exists(), "쪽지 스크린샷이 없습니다"

    shown = c.br.page.evaluate("document.body.innerText")
    for leak in ("김진욱", "101394", "박영희", "202020"):
        assert leak not in shown, f"그림에 '{leak}' 가 남았습니다"
    assert label in shown, "그림의 이름표가 .md 와 다릅니다"
    assert anon.sender(OTHER_SENDER) in shown, "아래 목록의 다른 보낸이를 안 가렸습니다"


def test_message_picture_is_skipped_when_anonymizing_fails(browser, tmp_path, monkeypatch):
    """다 못 가렸으면 찍지 않습니다. 반쯤 가린 그림은 가리지 않은 그림입니다."""
    monkeypatch.setattr(S, "_REPLACE", "() => {}")
    c = _crawler(browser, tmp_path, MESSAGE)
    monkeypatch.setattr(c, "_download_attachments", lambda d: C.Attachments())
    row = {"index": 0, "no": "1", "title": "자료 탑재 알림", "date": "2026-11-13",
           "has_file": False, "sender": RAW_SENDER}
    out = tmp_path / "msg"
    out.mkdir()
    c._open_message(row, anon.sender(RAW_SENDER), out, "key-msg2", "RECEIVE")

    md = next(out.glob("*.md"))
    assert md.exists(), "익명화 실패로 글까지 잃었습니다"
    assert not md.with_suffix(".png").exists(), "가리지 못한 그림을 남겼습니다"
    assert c.stats.shots_failed == 1


def test_non_message_pages_are_not_rewritten(browser, tmp_path):
    """게시글 작성자(교수)는 .md 에도 그대로 있습니다. 화면을 고치면 안 됩니다."""
    html = PAGE.replace("1주차: 오리엔테이션", f"작성자 {RAW_SENDER}")
    c = _crawler(browser, tmp_path, html)
    c.save_page_text(COURSE, "syllabus")
    assert RAW_SENDER in c.br.page.evaluate("document.body.innerText")
