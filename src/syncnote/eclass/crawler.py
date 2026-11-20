"""eClass 수집기.

동작 원리
---------
1) 메인 화면의 '내 강의실' select 에서 과목 목록을 읽습니다.
2) 과목마다 설정에서 켜 둔 메뉴를 돕니다.
     - URL 을 직접 만들 수 있는 메뉴는 goto 로 이동 (빠름)
     - 만들 수 없는 메뉴(강의계획서/과목정보/성적)는 좌측 메뉴 링크를 클릭
3) 게시판은 목록의 각 행을 '클릭'해 상세로 들어갑니다.
   (JS form POST 라서 URL 로는 못 감)
4) 본문은 마크다운으로, 첨부는 파일로 저장합니다.
5) 이미 받은 글은 건너뜁니다. 판단 기준은 (과목, 메뉴, 글번호, 제목, 작성일)
   의 해시입니다. 교수가 글을 수정하면 작성일이 바뀌지 않는 경우가 있어
   첨부는 내용 해시로 한 번 더 거릅니다.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get as get_logger
from ..paths import Layout, safe_name, unique_path
from ..state import Store, sha256_file, sha256_text
from . import anon, filedown, singlefile, snapshot
from .browser import EclassBrowser
from .urls import MENU_LABELS, MENU_LINK_TEXTS, RISKY_MENUS, Site

log = get_logger("eclass.crawler")

# 게시판 목록 테이블
BOARD_TABLE = "table.boardListBasic"
BOARD_ROW = f"{BOARD_TABLE} tbody tr"
BOARD_TITLE_LINK = "a.boardTitleNcontent, a.TITLE_ORIGIN, td.textLeft a"
# 페이지네이션. paginator3000 이 그립니다.
#   <div class="paginator fullsize" id="paginator3"> ... <span><strong>1</strong></span>
#   <div class="paginator_pages">3 pages</div>
PAGINATOR = "div.paginator"
PAGINATOR_PAGES = "div.paginator_pages"
MAX_BOARD_PAGES = 50          # 안전장치. 이보다 많으면 뭔가 잘못된 것입니다.

# 상세 화면에서 본문이 들어가는 곳 후보
DETAIL_BODY = (
    "div.boardViewContent", "div.viewContent", "div.bbsContent",
    "td.boardContents", "div#bodyContent div.cont", "div#bodyContent",
)
# 첨부가 아닌 버튼. 클릭하면 목록으로 나가거나 글을 지웁니다.
SKIP_LINK_TEXTS = frozenset({"목록", "수정", "삭제", "답글", "이전글", "다음글", "인쇄",
                             "목록보기", "제출", "제 출", "과제제출", "취소", "글쓰기"})

# 절대 누르면 안 되는 것. 과제 화면에는 첨부 옆에 제출 버튼이 있습니다.
#   <a class="btn small" onclick="submitReport('REPT_...')">과제제출</a>
#   <a class="btn btn-blue" onclick="goSave();">제 출</a>
# 빈 과제를 제출하면 교수에게 '제출함' 으로 보이고 되돌릴 수 없습니다.
# 첨부 선택자가 이것들을 잡을 일은 지금은 없지만, 선택자 폴백이
# 넓어서 한 번만 어긋나도 되돌릴 수 없는 일이 일어납니다. 눌리는
# 자리에서 한 번 더 막습니다.
NEVER_CLICK = (
    "gosave", "submitreport", "savereport", "uploaderclick", "submit(",
    "delete", "remove", "addboardcontents", "updateboardcontents",
    "replyboardcontents", "etest", "survey", "forum",
)


def _is_unsafe_click(target: str) -> bool:
    """onclick/href 에 되돌릴 수 없는 동작이 들어 있는지."""
    low = (target or "").lower().replace(" ", "")
    return any(bad in low for bad in NEVER_CLICK)

# 목록의 '파일' 칸에 붙는 첨부 표시. 어떤 아이콘 클래스가 쓰이는지는
# 사이트 스프라이트(icon_button.css)에서 뽑은 후보를 전부 넣었습니다.
# 덤프에 게시판 목록 HTML 이 없어 '이 중 어느 것' 인지는 확인하지
# 못했습니다. 틀려도 수집은 됩니다 — has_file 은 표시용 힌트일 뿐,
# 첨부를 실제로 찾는 것은 상세 화면의 ATTACHMENT 선택자입니다.
FILE_ICON = (
    ".icon-data-color, .icon-disk, .icon-disk-black, .icon-disk-white, "
    ".icon-save-color, .icon-download-color, .icon-download-black, "
    ".icon-filelist-black, .icon-filelist-white, "
    'img[alt*="첨부"], img[src*="file"], img[src*="disk"]'
)

# 첨부 링크 후보. 맨 앞이 이 사이트에서 실제로 쓰는 형태입니다 —
#   <a class="btn" onclick="fileDown('…','…','…');">교안</a>
# 나머지 다섯은 다른 화면을 대비한 폴백입니다. 2026-11 덤프의 강의
# 목록 페이지에서 확인한 결과 폴백은 전부 0개를 잡았고, 첫 줄만
# 14개(교안 PDF 전부)를 잡았습니다. 'fileDown' 에는 'ownload' 가
# 들어 있지 않아서 a[onclick*='ownload'] 로는 영영 못 찾습니다.
ATTACHMENT = (
    # 게시글 상세: <span onclick="fileDownload('이름','저장명','디렉터리')">
    # 강의 목록  : <a class="btn" onclick="fileDown('이름','이름','경로')">
    # 둘은 함수도 인자 뜻도 다릅니다. 태그도 다릅니다 — 상세 쪽은
    # <a> 가 아니라 <span> 이라서 'a[...]' 로 시작하는 선택자로는
    # 영원히 못 찾습니다.
    # 'fileDownload' 는 'fileDown' 을 포함하므로 이 한 줄이 둘 다 잡습니다.
    # 둘은 엔드포인트가 다릅니다(fileDown / fileDownServlet). 어느 쪽인지는
    # filedown.parse 가 함수 이름을 보고 가립니다.
    # 둘을 따로 적고 순서를 두면, 한 화면에 섞여 있을 때 먼저 걸린 쪽만
    # 받고 나머지를 조용히 흘립니다(_attachment_locators 는 처음 걸린
    # 선택자 하나만 돌려줍니다). 어느 함수인지는 filedown.parse 가 가립니다.
    "[onclick*='fileDown']",
    "a[onclick*='ownload']", "a[href*='ownload']",
    "ul.fileList a", "div.fileBox a", "td.file a", "a.fileDown",
)

# 강의 목록(/Lesson.do viewLessonContentsList) 구조.
#   <div id="listBox"><div class="listContent">
#     <dl class="element"><dt><h4>1. 1주차 … </h4></dt>
#       <dd><div class="cont"><table class="boardListBasic">…</table></div></dd>
LESSON_WEEK = "#listBox dl.element"

# 과제 목록의 '제출정보보기' 버튼.
#   <a class="btn small" onclick="javascript:submitReportView('REPT_...','N');">
# submitReportView 는 **열람 전용**입니다(폼 cmd 를 viewReportSubmit 으로 POST).
# 과제를 실제로 내는 submitReport(...) / 과제제출 과는 다릅니다 — 글자가
# 'submitReport' 까지 겹치므로, 'View(' 를 요구해 그 둘을 확실히 가릅니다.
# 이 선택자로 잡힌 것만 눌러 상세(제출정보)로 들어갑니다.
ASSIGN_DETAIL_LINK = "a[onclick*='submitReportView(']"
# 상세(제출정보) 화면의 라벨/값 표. 기본정보·제출정보 두 표가 같은 클래스입니다.
ASSIGN_DETAIL_TABLE = "table.boardListWrite"
_REPORT_ID_RE = re.compile(r"submitReportView\(\s*'([^']+)'")

# 쪽지함. 칸 순서가 게시판과 달라서(맨 앞이 체크박스) 따로 읽습니다.
#   [체크박스] [번호] [보낸이] [제목] [파일] [읽음] [받은 날짜]
MESSAGE_ROW = "table.boardListBasic tbody tr"
MESSAGE_LINK = "td.textLeft a"
# 첨부는 DWR 로 zip 을 만든 뒤 콜백에서 fileDownload 를 부릅니다.
# onclick 에 주소가 없으므로 이것만은 눌러서 받아야 합니다.
MESSAGE_FILE = "a[onclick*='compressFile'], i.icon-message-color, i[class*='message']"
MAX_MESSAGE_PAGES = 30

# 계정 레벨(과목 무관) 메뉴. 전부 실제 상단 내비(2026-11)에서 확인한 고정
# ID 의 moveMenu 또는 menuPage2 입니다 — 추측 ID 를 열거하지 않습니다(WAF 로깅).
# 내강의실 부모 menu_00026 / 학습도우미 부모 menu_00029.
ACCOUNT_MENUS: tuple[tuple[str, str], ...] = (
    ("학습시간표", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00026&mainDTO.menuId=menu_00252"),
    ("수강과목", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00026&mainDTO.menuId=menu_00031"),
    ("공개강좌", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00026&mainDTO.menuId=menu_00035"),
    ("학습도우미홈", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00029&mainDTO.menuId=menu_00247"),
    ("도우미공지", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00029&mainDTO.menuId=menu_00237"),
    ("도우미질의응답", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00029&mainDTO.menuId=menu_00249"),
    ("행사안내", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00029&mainDTO.menuId=menu_00250"),
    ("자주찾는질문", "/Main.do?cmd=moveMenu&mainDTO.parentMenuId=menu_00029&mainDTO.menuId=menu_00215"),
    ("커뮤니티", "/Main.do?cmd=menuPage2"),
)


@dataclass
class Course:
    course_id: str
    name: str
    professor: str = ""
    open_type: str = "normal"

    @property
    def slug(self) -> str:
        return safe_name(f"{self.name}")


@dataclass
class Stats:
    courses: int = 0
    pages: int = 0
    posts: int = 0
    files: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    # 아래 셋은 '아무것도 못 받았는데 성공으로 끝나는' 것을 잡기 위한 것입니다.
    boards_seen: int = 0                       # 들어간 게시판 수
    boards_empty: int = 0                      # 그중 0건이던 것
    menus_missing: dict[str, int] = field(default_factory=dict)
    shots: int = 0                             # .md 옆에 남긴 스크린샷
    # 첨부가 달릴 수 있는 글(게시판 상세·쪽지)만 셉니다. posts 에는
    # 강의목록·과제·목록 같은 화면 글자도 들어가서, 그걸로 '첨부 0' 을
    # 따지면 화면 하나만 바뀐 날도 경고가 뜹니다(2026-11-13: 글 1, 530 B).
    entries: int = 0
    files_known: int = 0                       # 이미 받아 둬서 이번엔 안 받은 첨부
    shots_failed: int = 0                      # 찍으려다 못 찍은 것(익명화 실패 포함)
    html_saved: int = 0                        # 한 파일짜리 전체 HTML 캡처 수
    html_failed: int = 0                       # 전체 HTML 캡처 실패 수

    def line(self) -> str:
        base = (f"과목 {self.courses}개 / 페이지 {self.pages} / 글 {self.posts} / "
                f"첨부 {self.files} / 건너뜀 {self.skipped} / 스크린샷 {self.shots}")
        if self.shots_failed:
            base += f"(실패 {self.shots_failed})"
        return base + (f" / 오류 {len(self.errors)}" if self.errors else "")

    def suspicions(self) -> list[str]:
        """숫자만 보면 성공인데 실제로는 아무것도 못 받은 경우를 짚습니다.

        2026-11-12 실행이 정확히 그랬습니다 — 9개 과목을 돌고 '글 16 /
        첨부 0' 으로 끝났고, 게시판 27곳이 전부 '글이 없습니다' 였으며,
        '과목정보' 는 9개 과목 모두에서 메뉴를 찾지 못했습니다. 그런데도
        작업은 성공(exit 0)으로 끝났습니다. 교수가 안 올린 것과 우리가
        못 찾는 것은 로그만 봐서는 똑같이 생겼습니다.
        """
        out: list[str] = []
        if self.courses >= 2 and self.boards_seen >= 2 and self.boards_empty == self.boards_seen:
            out.append(
                f"게시판 {self.boards_seen}곳이 전부 0건입니다. 과목이 {self.courses}개인데 "
                "한 곳도 글이 없는 것은 드문 일입니다. 주소나 목록 선택자가 "
                "어긋났을 수 있습니다."
            )
        if self.entries >= 3 and self.files == 0 and self.files_known == 0:
            out.append(
                f"게시글·쪽지 {self.entries}건을 저장했는데 첨부는 0개입니다. 첨부 링크를 "
                "못 찾고 있을 수 있습니다(화면마다 fileDown / fileDownload 로 다릅니다)."
            )
        for menu, n in sorted(self.menus_missing.items()):
            if n >= max(2, self.courses):
                out.append(
                    f"'{menu}' 메뉴를 {n}개 과목 전부에서 찾지 못했습니다. "
                    "과목마다 없는 메뉴가 아니라, 주소나 링크 텍스트가 바뀐 것일 수 있습니다."
                )
        return out


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", html.unescape(text or "")).strip()


class PageReadError(RuntimeError):
    """화면을 읽지 못했습니다. 글이 0건인 것과 구분해야 합니다."""


def _js_literal(value: Any) -> str:
    """파이썬 값을 JS 소스에 그대로 박아 넣을 리터럴로."""
    return json.dumps(value, ensure_ascii=False)


def _eval_json(page, expression: str, **literals: Any) -> Any:
    """evaluate 를 **문자열로만** 주고받습니다.

    **인자를 넘기지 않습니다.** 2026-11-12 러너 기록이 이렇습니다.

        인자 없음  + 복합 반환  -> 정상 (list_courses 는 9과목을 찾았음)
        문자열 인자 + 복합 반환  -> 조용히 None (read_lesson_weeks)
        리스트/객체 인자         -> TypeError: refs.set is not a function

    즉 터지는 쪽은 **인자** 입니다. playwright 의 역직렬화기
    (coreBundle.js parseSerializedValue)가 인자를 풀면서
    `refs.set(value.id, result)` 를 부르는데, 러너의 playwright 에서는
    그 refs 가 Map 이 아닙니다. 리스트를 객체로 바꿔도 같은 가지입니다.

    그래서 값은 인자로 보내지 않고 **JS 소스에 리터럴로 박습니다**
    (_js_literal). 선택자는 우리 상수라 외부 입력이 아닙니다. 결과도
    JSON.stringify 로 받아 문자열로만 건넵니다. 이러면 playwright
    버전과 무관하게 동작합니다.
    """
    for name, value in literals.items():
        token = f"__{name.upper()}__"
        if token not in expression:
            raise PageReadError(f"JS 안에 {token} 자리가 없습니다(오타?).")
        expression = expression.replace(token, _js_literal(value))
    left = re.search(r"__[A-Z_]+__", expression)
    if left:
        raise PageReadError(f"JS 안에 채우지 못한 자리가 있습니다: {left.group(0)}")
    try:
        raw = page.evaluate(expression)
    except Exception as exc:
        raise PageReadError(str(exc)) from exc
    if raw is None:
        raise PageReadError("페이지가 아무것도 돌려주지 않았습니다.")
    if not isinstance(raw, str):
        raise PageReadError(f"문자열을 기대했는데 {type(raw).__name__} 이 왔습니다.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PageReadError(f"결과를 읽지 못했습니다: {exc}") from exc


@dataclass
class Attachments:
    """첨부 수집 결과.

    '받은 것' 만 돌려주면 호출부가 실패를 알 수 없습니다. 그러면
    글은 '완료' 로 기록되고, 다음 실행은 그 글을 건너뛰어 실패한
    첨부를 영영 다시 시도하지 않습니다.
    """

    saved: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # 내용이 같은 파일을 이미 받아 둬서 건너뛴 수. saved 에는 안
    # 들어갑니다. 이걸 안 세면 두 번째 실행부터 '교안 0개' 와
    # '이미 다 받음' 이 똑같이 0 으로 보입니다.
    #
    # stats.skipped 는 실행 전체 누적(다른 메뉴 포함)이고, 이쪽은
    # 이 페이지·이 호출 한정입니다. 판정은 반드시 지역 카운터로
    # 하세요 — stats.skipped 로 판정하면 다른 메뉴의 수가 섞입니다.
    known: int = 0
    # 화면에 있던 누를 만한 것의 수. 0 이면 '첨부가 애초에 없는 화면'
    # 이고, 0 이 아닌데 아무것도 못 받았으면 그때가 진짜 문제입니다.
    candidates: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def __len__(self) -> int:         # 기존 len(files) 호출부 호환
        return len(self.saved)

    def __iter__(self):
        return iter(self.saved)


class Crawler:
    def __init__(
        self,
        browser: EclassBrowser,
        site: Site,
        layout: Layout,
        store: Store,
        *,
        term: str,
        menus: dict[str, bool],
        risky: dict[str, Any] | None = None,
        menu_id_overrides: dict[str, str] | None = None,
        message_boxes: Iterable[str] = ("RECEIVE",),
        screenshots: bool = True,
        capture_html: bool = True,
    ) -> None:
        self.br = browser
        self.screenshots = bool(screenshots)
        self.capture_html = bool(capture_html)
        self._owner: str | None = None   # 로그인 본인 이름(마스킹용), 지연 추출
        # 수집하며 '구조적으로' 알게 된 사람 이름들(작성자 열·이름 열·보낸이).
        # 사번이 안 붙어 정규식으론 못 잡는 '맨 이름' 이라도, 이 레지스트리에
        # 있으면 자유 본문·스크린샷·HTML·파일명 어디서든 그 문자열만 정확히
        # 가립니다. 사전이 아니라 '본 적 있는 이름' 이라, 못 본 이름은 못 가립니다.
        self._known_names: set[str] = set()
        self.site = site
        self.layout = layout
        self.store = store
        self.term = term
        self.menus = dict(menus)
        self.risky = dict(risky or {})
        self.menu_id_overrides = dict(menu_id_overrides or {})
        self.message_boxes = [str(b).upper() for b in message_boxes]
        self.stats = Stats()

    # -- 메뉴 선택 ---------------------------------------------------
    def enabled_menus(self) -> list[str]:
        """위험 메뉴는 옵트인이 두 겹(전체 동의 + 개별)일 때만 켜집니다."""
        out = [k for k, v in self.menus.items()
               if v and k not in RISKY_MENUS and k != "message"]
        if self.risky.get("i_understand_the_risk"):
            for key in RISKY_MENUS:
                if self.risky.get(key):
                    out.append(key)
                    log.warning(
                        "위험 메뉴 '%s' 가 켜져 있습니다. 목록 제목만 읽고 상세에는 진입하지 않습니다.",
                        MENU_LABELS.get(key, key),
                    )
        return out

    # -- 과목 목록 ---------------------------------------------------
    def list_courses(self) -> list[Course]:
        from .urls import parse_course_option

        self.br.goto(self.site.home)
        options = self.br.page.evaluate(
            """() => Array.from(
                   document.querySelectorAll("select[name='courseDTO.courseId'] option")
               ).map(o => ({value: o.value, text: o.textContent.trim()}))"""
        )
        courses: list[Course] = []
        for opt in options:
            parsed = parse_course_option(opt.get("value", ""))
            if not parsed:
                continue
            cid, prof, open_type = parsed
            courses.append(Course(cid, _clean(opt.get("text", "")) or cid, prof, open_type))

        if not courses:
            self.stats.errors.append(
                "과목 목록이 비어 있습니다. 로그인은 됐지만 select 선택자가 바뀌었을 수 있습니다. "
                "`python -m syncnote eclass probe` 를 돌려 보세요."
            )
        return courses

    def filter_courses(self, courses: list[Course], include: list[str],
                       exclude: list[str]) -> list[Course]:
        def hit(course: Course, needle: str) -> bool:
            hay = f"{course.name} {course.course_id}".lower()
            return needle.lower() in hay

        def matches(course: Course, needles: list[str]) -> bool:
            return any(hit(course, str(n).strip()) for n in needles if str(n).strip())

        if include:
            # 1) 이름 일부로 걸리는 셀렉터는 예전처럼 걸리는 과목 전부.
            # 2) 하나도 안 걸린 셀렉터만 퍼지 매칭(띄어쓰기·음절 순서 무관)으로
            #    최고 일치 과목 하나. 임계값 미만이면 버리고, 전체로 폴백하지 않음.
            from . import coursematch

            needles = [str(n).strip() for n in include if str(n).strip()]
            picked: set[str] = set()
            fuzzy: list[str] = []
            for n in needles:
                hits = [c for c in courses if hit(c, n)]
                if hits:
                    picked.update(c.course_id for c in hits)
                else:
                    fuzzy.append(n)
            if fuzzy:
                res = coursematch.select(courses, fuzzy, id_of=lambda c: c.course_id,
                                         name_of=lambda c: c.name)
                for line in res.report:
                    log.info("과목 매칭: %s", line)
                for m in res.matches:
                    if m.course is None:
                        self.stats.errors.append(f"과목 셀렉터 매칭 실패: {m.selector!r}")
                picked.update(c.course_id for c in res.selected)
            courses = [c for c in courses if c.course_id in picked]
        if exclude:
            courses = [c for c in courses if not matches(c, exclude)]
        return courses

    # -- 진입 --------------------------------------------------------
    # 강의실 홈이 /Lesson.do 인 개설 유형. 사이트의 viewCourse() 가
    #   if (openType !== "open" && openType !== "mooc") -> /Course.do
    # 로 갈라지므로 두 값을 똑같이 다뤄야 합니다. 'mooc' 를 빠뜨리면
    # 그 과목만 엉뚱한 화면에 들어가 조용히 0건으로 끝납니다.
    # 'bnormal'(법정의무교육 등)은 분기에 없으므로 일반 과목과 같습니다.
    LESSON_HOME_TYPES = frozenset({"open", "mooc"})

    def open_course(self, course: Course) -> bool:
        url = (self.site.open_course_home(course.course_id)
               if course.open_type in self.LESSON_HOME_TYPES
               else self.site.course_home(course.course_id))
        try:
            self.br.goto(url)
            return True
        except Exception as exc:
            msg = f"{course.name}: 강의실 진입 실패 - {exc}"
            log.error(msg)
            self.stats.errors.append(msg)
            return False

    def open_menu(self, course: Course, key: str) -> bool:
        """URL 로 가거나, 안 되면 좌측 메뉴 링크 텍스트로 클릭합니다."""
        url = self.site.menu_url(course.course_id, key, self.menu_id_overrides)
        if url:
            try:
                self.br.goto(url)
                self.stats.pages += 1
                return True
            except Exception as exc:
                log.debug("%s URL 이동 실패, 클릭으로 재시도: %s", key, exc)

        # 클릭 경로: 강의실 홈으로 돌아가 메뉴 링크를 찾습니다.
        if not self.open_course(course):
            return False
        for text in MENU_LINK_TEXTS.get(key, ()):
            locator = self.br.page.get_by_role("link", name=text, exact=False).first
            try:
                if locator.count() == 0:
                    continue
                locator.click()
                self.br.page.wait_for_load_state("domcontentloaded", timeout=self.br.timeout_ms)
                self.stats.pages += 1
                return True
            except Exception:
                continue
        label = MENU_LABELS.get(key, key)
        self.stats.menus_missing[label] = self.stats.menus_missing.get(label, 0) + 1
        log.info("%s: '%s' 메뉴가 없습니다(과목에 해당 메뉴 미개설).", course.name, label)
        return False

    # -- 저장 --------------------------------------------------------
    def _dir(self, course: Course, key: str) -> Path:
        d = self.layout.menu_dir(self.term, course.slug, MENU_LABELS.get(key, key))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot(self, md: Path | None, *, senders: Iterable[str] | None = None,
                  known_names: Iterable[str] = ()) -> None:
        """방금 쓴 `md` 옆에 지금 화면을 남깁니다.

        `.md` 를 실제로 쓴 자리에서만 부릅니다. 내용이 그대로라 `.md` 를
        건너뛴 실행에서는 그림도 건너뜁니다 — 그래야 그림과 글이 같은
        시점의 것입니다.
        """
        if md is None:
            return
        if self.screenshots:
            # 이번 호출의 known_names 에 더해, 그동안 구조적으로 알게 된 모든
            # 이름을 함께 넘깁니다 — 다른 메뉴에서 본 이름이 이 화면에 있어도
            # 가립니다.
            merged = [*known_names, *self._known_names]
            if snapshot.take(self.br.page, md, senders=senders,
                             own_name=self._owner_name(),
                             known_names=merged) is not None:
                self.stats.shots += 1
            else:
                self.stats.shots_failed += 1
        if self.capture_html:
            self._capture_full_html(md)

    def _note_names(self, values: Iterable[str]) -> None:
        """구조적으로 알게 된 사람 이름을 레지스트리에 모읍니다(2자 이상·글자 포함).

        `이름 (사번)` 꼴이면 '이름' 만 떼어 함께 담습니다 — 본문엔 사번 없이
        이름만 나오는 경우가 많기 때문입니다. 숫자·이메일·전화는 이미 정규식이
        잡으므로 담지 않습니다.
        """
        for v in values:
            v = (v or "").strip()
            if not v:
                continue
            m = anon._SENDER.match(v)
            if m:
                v = m.group("name").strip()
            if len(v) >= 2 and any(ch.isalpha() for ch in v) and not v.isdigit():
                self._known_names.add(v)

    def _mask_names(self, own: str = "") -> tuple[str, ...]:
        """자유 본문용(mask_pii 의 own_names 자리): 본인 이름 + 레지스트리."""
        names = set(self._known_names)
        own = (own or self._owner or "").strip()
        if len(own) >= 2:
            names.add(own)
        return tuple(names)

    def _owner_name(self) -> str:
        """상단 프로필의 로그인 이름(마스킹용). 한 번 찾으면 캐시."""
        if self._owner:
            return self._owner
        for sel in ("li.w230 dl dt.fontB", "dt.fontB"):
            try:
                loc = self.br.page.locator(sel).first
                if loc.count() and (t := loc.inner_text(timeout=600).strip()):
                    self._owner = t.split("(")[0].strip()
                    self._note_names([self._owner])
                    return self._owner
            except Exception:
                continue
        return ""

    def _capture_full_html(self, md: Path) -> None:
        """md 옆에 자체완결 전체 HTML(한 파일) 을 남깁니다. 개인정보 마스킹.

        과제/첨부 '파일 다운로드' 와 별개입니다 — 그건 그대로 돕니다.
        캡처가 실패해도 .md 수집은 영향받지 않습니다.
        """
        own = self._owner_name()
        try:
            html = singlefile.capture(self.br.page, own_names=self._mask_names(own))
        except Exception as exc:
            log.debug("전체 HTML 캡처 실패(%s): %s", md.name, exc)
            self.stats.html_failed += 1
            return
        try:
            md.with_suffix(".html").write_text(html, encoding="utf-8")
            self.stats.html_saved += 1
        except Exception as exc:
            log.debug("전체 HTML 저장 실패(%s): %s", md.name, exc)
            self.stats.html_failed += 1

    def save_page_text(self, course: Course, key: str, *, name: str | None = None) -> Path | None:
        """현재 페이지의 본문을 마크다운으로 저장합니다.

        강의계획서/과목정보/출석/성적처럼 '페이지 전체가 내용' 인 메뉴용입니다.
        """
        try:
            text = self.br.page.evaluate(
                """() => {
                     const el = document.querySelector('#bodyContent')
                             || document.querySelector('#container')
                             || document.body;
                     return el ? el.innerText : '';
                   }"""
            )
        except Exception as exc:
            log.warning("본문 추출 실패(%s/%s): %s", course.name, key, exc)
            return None

        text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        if not text:
            return None

        label = MENU_LABELS.get(key, key)
        digest = sha256_text(text)
        state_key = f"page:{course.course_id}:{key}"
        previous = self.store.data["downloaded"].get(state_key, {})
        if previous.get("sha256") == digest:
            self.stats.skipped += 1
            log.debug("변경 없음: %s / %s", course.name, label)
            return None

        out = self._dir(course, key) / safe_name(f"{name or label}.md")
        header = (
            f"# {course.name} - {label}\n\n"
            f"- 과목코드: `{course.course_id}`\n"
            f"- 담당: {course.professor}\n"
            f"- 수집시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n"
            f"- 출처: {self.br.page.url}\n\n---\n\n"
        )
        out.write_text(header + text + "\n", encoding="utf-8")
        self._snapshot(out)
        self.store.mark_downloaded(state_key, path=str(out), digest=digest)
        self.stats.posts += 1
        log.info("저장: %s / %s", course.name, label)
        return out

    # -- 학습 목차 ---------------------------------------------------
    # 학습 목차 화면은 한 번에 세 주차만 보여 주고 좌우 화살표
    #   <a href="javascript:viewNextPage('12','next');">
    # 로 창을 옮깁니다. 들어가자마자 보이는 것은 '이번 주' 근처 세 칸뿐이라,
    # 화면을 그대로 저장하면 16주 중 3주만 남습니다. 학기 중에는 그래도
    # 뭔가 저장되니 비어 보이지 않고, 그래서 빠진 줄도 모릅니다.
    CURRICULUM_TABLE = "#listBox table"
    CURRICULUM_PREV = "p.arrowLeft a, a[href*=\"viewNextPage\"][href*=prev]"
    CURRICULUM_NEXT = "p.arrowRight a, a[href*=\"viewNextPage\"][href*=next]"
    MAX_WEEK_STEPS = 30          # 학기가 이보다 길 수는 없습니다.

    def _curriculum_window(self) -> dict[str, Any] | None:
        """지금 보이는 세 주차를 주차별로 쪼개서 읽습니다."""
        try:
            return _eval_json(
                self.br.page,
                """() => {
                     const t = document.querySelector(__SEL__);
                     if (!t) return "null";
                     const weeks = Array.from(t.querySelectorAll('thead th[scope="col"]'))
                       .map(th => (th.innerText||'').replace(/\\s+/g,' ').trim());
                     if (!weeks.length) return "null";
                     const rows = Array.from(t.querySelectorAll('tbody tr')).map(tr => {
                       // 행 머리에는 이름 옆에 '더보기' 링크가 붙어 있어서
                       // innerText 를 그대로 쓰면 '강의영상 더보기' 가 됩니다.
                       const head = tr.querySelector('th .text') || tr.querySelector('th');
                       return {
                         label: (head ? head.innerText : '').replace(/\\s+/g,' ').trim(),
                         cells: Array.from(tr.querySelectorAll('td'))
                                  .map(td => (td.innerText||'').replace(/[ \\t]+/g,' ').trim()),
                       };
                     });
                     return JSON.stringify({weeks: weeks, rows: rows});
                   }""",
                sel=self.CURRICULUM_TABLE,
            )
        except PageReadError as exc:
            log.debug("학습목차 창 읽기 실패: %s", exc)
            return None

    def _step_week(self, selector: str) -> bool:
        """화살표를 눌러 주차 창을 옮깁니다. 실제로 바뀌어야 True.

        화살표는 viewNextPage() 를 불러 **폼을 POST 하고 페이지를 새로
        띄웁니다.** 그래서 클릭과 새 화면 사이에 navigation 이 있습니다.
        그냥 클릭하고 domcontentloaded 를 기다리면, 아직 옛 화면인 채로
        읽거나 locator 가 떨어져 나가 예외가 납니다. 둘 다 조용히
        '못 넘겼다' 로 끝나서, 2026-11-12 실행에서 16주 과목 아홉 개가
        전부 첫 세 주차만 저장됐습니다.
        """
        page = self.br.page
        before = self._curriculum_window()
        before_weeks = before["weeks"] if before else None
        loc = page.locator(selector).first
        try:
            if loc.count() == 0:
                log.debug("주차 이동: 화살표가 없습니다(%s)", selector)
                return False
        except Exception as exc:
            log.debug("주차 이동: 화살표를 세지 못했습니다(%s): %s", selector, exc)
            return False

        try:
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=self.br.timeout_ms):
                loc.click(timeout=self.br.timeout_ms)
        except Exception as exc:
            # navigation 이 안 일어나는 화면일 수도 있습니다(AJAX 교체 등).
            log.debug("주차 이동: navigation 을 못 봤습니다(%s): %s", selector, exc)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=self.br.timeout_ms)
            except Exception:
                pass
        page.wait_for_timeout(300)

        after = self._curriculum_window()
        if not after:
            log.debug("주차 이동: 옮긴 뒤 표를 찾지 못했습니다(%s)", selector)
            return False
        # 눌렸지만 아무 일도 없었던 경우를 성공으로 치면 영원히 돕니다.
        if after["weeks"] == before_weeks:
            log.debug("주차 이동: 눌렀지만 주차가 그대로입니다(%s)", selector)
            return False
        return True

    def collect_curriculum(self, course: Course) -> dict[str, list[tuple[str, str]]]:
        """맨 앞 주차까지 되감은 뒤 끝까지 넘기며 전 주차를 모읍니다."""
        found: dict[str, list[tuple[str, str]]] = {}

        def take() -> None:
            win = self._curriculum_window()
            if not win:
                return
            for i, week in enumerate(win["weeks"]):
                if not week or week in found:
                    continue
                items = [
                    (row["label"], row["cells"][i])
                    for row in win["rows"]
                    if i < len(row["cells"]) and row["cells"][i]
                ]
                found[week] = items

        take()
        first = dict(found)
        rewound = 0
        while rewound < self.MAX_WEEK_STEPS and self._step_week(self.CURRICULUM_PREV):
            rewound += 1
            take()
        advanced = 0
        while advanced < self.MAX_WEEK_STEPS and self._step_week(self.CURRICULUM_NEXT):
            advanced += 1
            take()

        if rewound == 0 and advanced == 0 and len(found) <= 3:
            # [이수필수], 학생생활관처럼 원래 1주차짜리 단일 콘텐츠인
            # 과목이 있습니다. 화살표가 없는 것이 정상이라, 이걸 경고로
            # 찍으면 진짜 실패 사이에 노이즈로 섞입니다.
            # 2026-11-12 실행에서 9과목 전부 '3주차' 로 끝났습니다. 16주
            # 과목인데 첫 창만 읽었다는 뜻입니다. 화살표를 못 찾았는지,
            # 눌렀는데 화면이 안 바뀌었는지가 로그로 구분돼야 합니다.
            page = self.br.page
            try:
                prev_n = page.locator(self.CURRICULUM_PREV).count()
                next_n = page.locator(self.CURRICULUM_NEXT).count()
            except Exception as exc:
                prev_n = next_n = f"셀 수 없음({exc})"
            single = len(first) <= 1 and prev_n == 0 and next_n == 0
            log.log(
                logging.INFO if single else logging.WARNING,
                "%s / 학습목차: 주차를 한 칸도 못 넘겼습니다(%d주차만). "
                "화살표 개수 prev=%s next=%s. %s",
                course.name, len(first), prev_n, next_n,
                "1주차짜리 단일 콘텐츠로 보입니다(정상)." if single else
                "0 이면 선택자가 틀린 것이고, 0 이 아니면 눌러도 화면이 안 바뀐 것입니다.",
            )

        if rewound >= self.MAX_WEEK_STEPS or advanced >= self.MAX_WEEK_STEPS:
            msg = (f"{course.name}/학습목차: 주차 이동이 {self.MAX_WEEK_STEPS}번을 넘었습니다. "
                   "일부 주차가 빠졌을 수 있습니다.")
            log.warning(msg)
            self.stats.errors.append(msg)
        return found

    def save_curriculum(self, course: Course) -> Path | None:
        weeks = self.collect_curriculum(course)
        if not weeks:
            log.info("%s / 학습목차: 주차 표를 찾지 못해 화면 전체를 저장합니다.", course.name)
            return self.save_page_text(course, "curriculum")

        lines: list[str] = []
        for week, items in weeks.items():
            lines.append(f"## {week}")
            lines.append("")
            if items:
                for label, cell in items:
                    lines.append(f"### {label}" if label else "###")
                    lines.append("")
                    lines.append(cell)
                    lines.append("")
            else:
                lines.append("(내용 없음)")
                lines.append("")
        body = "\n".join(lines).strip()

        digest = sha256_text(body)
        state_key = f"page:{course.course_id}:curriculum"
        if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
            self.stats.skipped += 1
            log.debug("변경 없음: %s / 학습목차", course.name)
            return None

        out = self._dir(course, "curriculum") / safe_name("학습목차.md")
        header = (
            f"# {course.name} - 학습목차\n\n"
            f"- 과목코드: `{course.course_id}`\n"
            f"- 담당: {course.professor}\n"
            f"- 수집시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n"
            f"- 주차 수: {len(weeks)}\n\n---\n\n"
        )
        out.write_text(header + body + "\n", encoding="utf-8")
        # 학습목차는 세 주차씩 넘겨 가며 모읍니다. 그림은 마지막으로 보던
        # 화면 하나뿐이고, 전 주차는 .md 에만 있습니다.
        self._snapshot(out)
        self.store.mark_downloaded(state_key, path=str(out), digest=digest)
        self.stats.posts += 1
        log.info("저장: %s / 학습목차 (%d주차)", course.name, len(weeks))
        return out

    # -- 과제 -------------------------------------------------------
    # 과제 목록(/Report.do viewReportInfoPageList)은 게시판이 아닙니다.
    # table.boardListBasic 이 아니라 dl.element + table.boardListInfo 라서
    # crawl_board 로 읽으면 행이 0건이고 "글이 없습니다" 로 끝납니다.
    # 과제가 19개 있어도 그렇습니다.
    #
    # 이 화면에는 제목 옆에 '과제제출' 버튼이, 그 폼에는 '제출' 버튼이
    # 있습니다. 첨부만 받고 그 둘은 절대 누르지 않습니다
    # (_is_unsafe_click / SKIP_LINK_TEXTS).
    def crawl_assignments(self, course: Course) -> None:
        blocks = self.read_lesson_weeks()
        out_dir = self._dir(course, "assignment")

        if not blocks:
            log.info("%s / 과제: 과제 블록을 찾지 못했습니다.", course.name)
            self.save_page_text(course, "assignment")
            return

        lines = [f"# {course.name} - 과제", ""]
        for b in blocks:
            lines += [f"## {b['title'] or '(제목 없음)'}", ""]
            for cells in b["rows"]:
                lines.append("- " + " | ".join(c for c in cells if c))
            if b.get("text"):
                lines += ["", b["text"]]
            lines.append("")
        body = "\n".join(lines).strip()

        digest = sha256_text(body)
        state_key = f"page:{course.course_id}:assignment"
        if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
            self.stats.skipped += 1
        else:
            md = out_dir / safe_name("과제.md")
            md.write_text(body + "\n", encoding="utf-8")
            self._snapshot(md)
            self.store.mark_downloaded(state_key, path=str(md), digest=digest)
            self.stats.posts += 1

        files = self._download_attachments(out_dir)

        # 목록만으로는 과제명·제출기간·제출여부·참고자료까지입니다. '제출상태'
        # (제출완료/과제제출일시)와 **제출한 첨부파일** 은 '제출정보보기'
        # (submitReportView=열람 전용)로 들어가는 상세 화면에만 있습니다.
        # 그래서 상세까지 들어가 그 값들을 함께 모읍니다. 실패해도 목록
        # 수집은 이미 끝나 있습니다.
        try:
            detail_n, sub_files = self.collect_assignment_submissions(course, out_dir)
        except Exception as exc:  # 상세 수집 실패가 목록 수집을 깨면 안 됩니다.
            log.warning("%s / 과제 제출정보 수집 실패: %s", course.name, exc)
            detail_n, sub_files = 0, 0

        log.info("%s / 과제: %d건, 참고자료 %d개, 제출정보 %d건, 제출첨부 %d개",
                 course.name, len(blocks), len(files), detail_n, sub_files)
        self.store.save()

    # 과제 목록의 한 과제 = dl.element 하나. 제목·제출기간·제출여부와,
    # '제출정보보기'(submitReportView) 의 reportInfoId 를 함께 읽습니다.
    def read_assignment_entries(self) -> list[dict[str, Any]]:
        raw = _eval_json(
            self.br.page,
            """() => {
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const out = Array.from(
                   document.querySelectorAll('#listBox dl.element')).map(dl => {
                   const h = dl.querySelector('dt h4');
                   const info = dl.querySelector('table.boardListInfo tbody tr');
                   const tds = info ? Array.from(info.querySelectorAll('td')) : [];
                   const due = tds.length ? squash(tds[0].innerText) : '';
                   const status = tds.length ? squash(tds[tds.length - 1].innerText) : '';
                   const view = dl.querySelector("a[onclick*='submitReportView(']");
                   const oc = view ? (view.getAttribute('onclick') || '') : '';
                   return { title: squash(h ? h.innerText : ''),
                            due: due, status: status, onclick: oc };
                 });
                 return JSON.stringify(out);
               }""",
        )
        if not isinstance(raw, list):
            raise PageReadError(f"과제 목록이 리스트가 아닙니다: {type(raw).__name__}")
        entries: list[dict[str, Any]] = []
        detail_index = 0
        for e in raw:
            oc = e.get("onclick") or ""
            m = _REPORT_ID_RE.search(oc)
            report_id = m.group(1) if m else ""
            entry = {
                "title": e.get("title", ""),
                "due": e.get("due", ""),
                "status": e.get("status", ""),
                "report_info_id": report_id,
                # 화면에서 '제출정보보기' 가 걸린 순서(nth). report_info_id 가
                # 있는 블록만 셉니다 — 그 순서로 링크를 눌러야 맞습니다.
                "detail_index": detail_index if report_id else -1,
            }
            if report_id:
                detail_index += 1
            entries.append(entry)
        return entries

    # 제출정보 상세 화면(viewReportSubmit)의 라벨/값 쌍을 순서대로 읽습니다.
    # 기본정보(과제명·제출기간·과제내용·참고자료)와 제출정보(제출자·과제설명·
    # 첨부파일·제출상태·과제제출일시)가 같은 class 의 두 표에 나뉘어 있습니다.
    def read_submission_detail(self) -> list[tuple[str, str]]:
        raw = _eval_json(
            self.br.page,
            """() => {
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const out = [];
                 for (const tb of document.querySelectorAll(__SEL__)) {
                   for (const tr of tb.querySelectorAll('tbody tr')) {
                     const th = tr.querySelector('th');
                     const td = tr.querySelector('td');
                     if (!th || !td) continue;
                     const label = squash(th.innerText);
                     if (!label) continue;
                     out.push([label, squash(td.innerText)]);
                   }
                 }
                 return JSON.stringify(out);
               }""",
            sel=ASSIGN_DETAIL_TABLE,
        )
        if not isinstance(raw, list):
            raise PageReadError(f"제출정보가 리스트가 아닙니다: {type(raw).__name__}")
        return [(str(p[0]), str(p[1])) for p in raw if isinstance(p, list) and len(p) == 2]

    def _return_to_assignment_list(self, course: Course) -> bool:
        """상세를 한 번 들어가면 목록이 사라집니다. 과제 목록으로 돌아갑니다."""
        return self.open_menu(course, "assignment")

    def collect_assignment_submissions(
            self, course: Course, out_dir: Path) -> tuple[int, int]:
        """'제출정보보기' 가 있는 과제마다 상세로 들어가 제출정보를 모읍니다.

        돌려주는 값: (상세를 읽은 과제 수, 받은 제출첨부 수).

        **안전**: 누르는 것은 submitReportView(열람 전용)뿐입니다. 과제제출
        /제출/goSave 는 선택자(ASSIGN_DETAIL_LINK)로도, 누르기 직전 재확인
        으로도 막습니다. 상세의 첨부는 기존 _download_attachments 가 받습니다
        (제출 버튼은 _is_unsafe_click 으로 거릅니다).
        """
        entries = [e for e in self.read_assignment_entries() if e["report_info_id"]]
        if not entries:
            return 0, 0

        sections: list[str] = []
        subfiles = 0
        done = 0
        for e in entries:
            # 목록으로 복귀(첫 과제도 포함 — 현재 화면이 상세일 수 있음).
            if not self._return_to_assignment_list(course):
                self.stats.errors.append(
                    f"{course.name}/과제: 제출정보 수집 중 목록 복귀 실패")
                break

            links = self.br.page.locator(ASSIGN_DETAIL_LINK)
            if e["detail_index"] < 0 or e["detail_index"] >= links.count():
                continue
            link = links.nth(e["detail_index"])
            # 누르기 직전 마지막 확인: 정말 열람 전용 링크인가.
            oc = link.get_attribute("onclick") or ""
            if "submitReportView(" not in oc or _is_unsafe_click(
                    oc.replace("submitReportView", "")):
                log.warning("%s/과제: 안전하지 않아 건너뜁니다: %s", course.name, oc[:60])
                continue

            if not self._enter_detail(link, f"과제 제출정보/{e['title']}"):
                self.stats.errors.append(
                    f"{course.name}/과제 '{e['title']}': 제출정보 상세에 못 들어갔습니다")
                continue

            pairs = self.read_submission_detail()
            fields = dict(pairs)  # 라벨 중복 시 뒤엣것(제출정보 쪽)이 남습니다
            title = fields.get("과제명") or e["title"]
            sections.append(self._submission_section(title, e, pairs))

            stem = safe_name(title, max_len=60) or f"과제{done + 1}"
            att = self._download_attachments(out_dir / "제출첨부" / safe_name(stem, max_len=60))
            subfiles += len(att)
            done += 1

        if sections:
            body = (f"# {course.name} - 과제 제출정보\n\n"
                    f"<!-- '제출정보보기'(열람 전용)로 들어가 모은 값입니다. "
                    f"수집 {datetime.now().astimezone():%Y-%m-%d %H:%M} -->\n\n"
                    + "\n".join(sections)).strip()
            digest = sha256_text(body)
            state_key = f"page:{course.course_id}:assignment_submit"
            if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
                self.stats.skipped += 1
            else:
                md = out_dir / safe_name("과제_제출정보.md")
                md.write_text(body + "\n", encoding="utf-8")
                self._snapshot(md)
                self.store.mark_downloaded(state_key, path=str(md), digest=digest)
                self.stats.posts += 1
        return done, subfiles

    @staticmethod
    def _submission_section(title: str, entry: dict[str, Any],
                            pairs: list[tuple[str, str]]) -> str:
        # 한 줄 값(표 라벨)과 본문(과제내용·과제설명)을 나눠 적습니다.
        long_labels = {"과제 내용", "과제설명", "참고 자료"}
        lines = [f"## {title}", ""]
        due = entry.get("due") or next(
            (v for k, v in pairs if k == "제출기간"), "")
        if due:
            lines.append(f"- 제출기간: {due}")
        for label, value in pairs:
            if label in ("과제명", "제출기간") or label in long_labels:
                continue
            lines.append(f"- {label}: {value}" if value else f"- {label}:")
        if not any(k == "제출상태" for k, _ in pairs) and entry.get("status"):
            lines.append(f"- 제출여부: {entry['status']}")
        for label in ("참고 자료", "과제 내용", "과제설명"):
            value = next((v for k, v in pairs if k == label), "")
            if value:
                lines += ["", f"### {label}", "", value]
        lines.append("")
        return "\n".join(lines)

    # -- 강의 목록(교안) ---------------------------------------------
    # 이 화면이 사용자가 제일 원하는 것 — 주차별 교안 PDF 가 여기 있습니다.
    # 예전에는 save_page_text 로 보내서 표의 '글자' 만 저장하고 PDF 는
    # 한 장도 받지 않았습니다. 화면에는 파일 이름이 보이니까 저장된 줄
    # 알기 쉬운데, 실제로는 목록만 있었습니다.
    def read_lesson_weeks(self) -> list[dict[str, Any]]:
        """주차(또는 과제) 블록을 읽습니다.

        dt 안에는 제목(h4) 옆에 버튼 묶음(ul.btnBox)이 있어서, dt 를
        통째로 읽으면 '과제제출' 같은 글자가 제목에 붙습니다.
        표가 아닌 dd 는 설명문입니다(과제의 '참고 자료' 등).
        """
        blocks = _eval_json(
            self.br.page,
            """() => {
                 const sel = __SEL__;
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const out = Array.from(document.querySelectorAll(sel)).map(dl => {
                   const h = dl.querySelector('dt h4') || dl.querySelector('dt');
                   const rows = Array.from(dl.querySelectorAll('table tbody tr'))
                     .map(tr => Array.from(tr.querySelectorAll('td'))
                            .map(td => squash(td.innerText)))
                     .filter(cells => cells.length);
                   const text = Array.from(dl.querySelectorAll('dd'))
                     .filter(dd => !dd.querySelector('table'))
                     .map(dd => (dd.innerText || '').trim())
                     .filter(Boolean).join('\\n').trim();
                   return { title: squash(h ? h.innerText : ''), rows: rows, text: text };
                 });
                 return JSON.stringify(out);
               }""",
            sel=LESSON_WEEK,
        )
        if not isinstance(blocks, list):
            raise PageReadError(f"주차 목록이 리스트가 아닙니다: {type(blocks).__name__}")
        return blocks

    def lesson_row_clickables(
            self, limit: int = 12) -> tuple[int, list[dict[str, str]]] | None:
        """강의 항목 **줄 안에서** 누를 수 있는 것들 (개수, 앞의 limit 개).
        못 읽으면 None — 그때는 '없다' 로 치지 않습니다.

        2026-11-13 실행에서 법정의무교육(영상 1개) 과목이 '교안 0개'
        경고를 냈는데, 함께 찍은 덤프는 화면 전체에서 앞의 12개만
        보여서 상단 메뉴만 나왔습니다. 정작 강의 줄에 무엇이 있는지는
        안 나왔습니다. 그래서 범위를 강의 줄로 좁혀 봅니다.

        줄 안에 누를 것이 **아무것도 없으면** 받을 파일도 없습니다(정상).
        누를 것은 있는데 첨부 선택자가 하나도 못 잡았으면 그때가 링크
        모양이 바뀐 것입니다. "교안" 이라는 글자로 가르지 않습니다.
        """
        try:
            found = _eval_json(
                self.br.page,
                """() => {
                     const sel = __SEL__;
                     const out = [];
                     for (const tr of document.querySelectorAll(sel)) {
                       for (const el of tr.querySelectorAll('[onclick], a[href]')) {
                         const oc = el.getAttribute('onclick') || '';
                         const hr = el.getAttribute('href') || '';
                         const deadHref = !hr || hr === '#' || hr.startsWith('javascript:void');
                         if (!oc && deadHref) continue;
                         out.push({
                           tag: el.tagName,
                           onclick: oc.slice(0, 120),
                           href: deadHref ? '' : hr.slice(0, 120),
                           text: (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 40)
                         });
                       }
                     }
                     return JSON.stringify({ total: out.length, items: out.slice(0, __LIMIT__) });
                   }""",
                sel=f"{LESSON_WEEK} table tbody tr",
                limit=limit,
            )
        except PageReadError:
            return None
        if not isinstance(found, dict):
            return None
        return int(found.get("total") or 0), list(found.get("items") or [])

    def crawl_lecture_list(self, course: Course) -> None:
        weeks = self.read_lesson_weeks()
        out_dir = self._dir(course, "lecture_list")

        if weeks:
            lines = [f"# {course.name} - 강의목록", ""]
            for week in weeks:
                lines += [f"## {week['title'] or '(제목 없음)'}", ""]
                for cells in week["rows"]:
                    lines.append("- " + " | ".join(c for c in cells if c))
                lines.append("")
            body = "\n".join(lines).strip()
            digest = sha256_text(body)
            state_key = f"page:{course.course_id}:lecture_list"
            if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
                self.stats.skipped += 1
            else:
                md = out_dir / safe_name("강의목록.md")
                md.write_text(body + "\n", encoding="utf-8")
                self._snapshot(md)
                self.store.mark_downloaded(state_key, path=str(md), digest=digest)
                self.stats.posts += 1
        else:
            # 구조가 바뀌었을 수 있습니다. 표라도 글자로 남겨 둡니다.
            log.info("%s / 강의목록: 주차 블록을 찾지 못했습니다.", course.name)
            self.save_page_text(course, "lecture_list")

        files = self._download_attachments(out_dir)
        # 강의 항목 수. **첨부 선택자와 다른 경로**로 읽습니다.
        # 이게 핵심입니다 — 후보 수(files.candidates)만 보면
        # _attachment_locators 가 통째로 고장나도 "받을 게 없는
        # 과목" 으로 조용히 분류됩니다. 항목이 있는데 후보가 0 이면
        # 그때가 선택자 고장입니다.
        items = sum(len(w.get("rows") or []) for w in weeks)

        # 로그는 '이미 받음' 이 먼저 보여야 합니다. "교안 0개" 가
        # 앞에 오면 두 번째 실행마다 사용자가 선택자 고장으로
        # 오해합니다.
        if not files.saved and files.known:
            log.info("%s / 강의목록: 주차 %d개, 항목 %d개, 교안 %d개를 "
                     "이미 받아 뒀습니다(이번에 새로 받은 것 없음).",
                     course.name, len(weeks), items, files.known)
        else:
            log.info("%s / 강의목록: 주차 %d개, 항목 %d개, 교안 %d개"
                     "(새로 %d, 이미 %d)",
                     course.name, len(weeks), items,
                     len(files) + files.known, len(files), files.known)

        if files.failed:
            msg = (f"{course.name}/강의목록: 교안 {len(files.failed)}개를 "
                   f"받지 못했습니다 ({', '.join(files.failed[:3])})")
            log.error("%s", msg)
            self.stats.errors.append(msg)

        # '교안 0개' 는 여러 경우가 섞여 있습니다. 2026-11-13 수집에서
        # 7과목이 이 경고를 냈는데 **전부 정상**이었습니다. 정상이 매번
        # 경고로 뜨면 진짜 고장이 그 사이에 묻힙니다.
        got_any = bool(files.saved) or bool(files.known) or bool(files.failed)
        if not weeks or got_any:
            pass
        elif items == 0:
            # 주차 틀만 있고 강의 항목이 한 줄도 없습니다. 교수가
            # 강의목록을 안 쓰는 과목입니다(9과목 중 7과목).
            log.info("%s / 강의목록: 주차 %d개에 강의 항목이 하나도 "
                     "없습니다. 받을 교안이 없습니다(정상).",
                     course.name, len(weeks))
        elif (rows_click := self.lesson_row_clickables()) is not None \
                and rows_click[0] == 0:
            # 항목은 있지만 그 줄들 안에 누를 것이 하나도 없습니다.
            # 버튼이 없으니 받을 파일도 없습니다(영상만 있는 강의 등).
            # 선택자 고장이면 줄 안의 버튼은 그대로 있으므로 아래
            # 경고로 갑니다 — 이 둘은 섞이지 않습니다.
            log.info("%s / 강의목록: 강의 항목 %d개에 누를 버튼이 하나도 "
                     "없습니다. 받을 교안이 없습니다(정상).",
                     course.name, items)
        else:
            # 항목 줄에 버튼은 있는데 첨부 선택자가 하나도 못 잡았습니다.
            # 링크 모양이 바뀌었거나, 교안이 아닌 버튼(학습하기 등)만
            # 있는 강의입니다.
            #
            # "교안" 이라는 글자로는 가르지 않습니다 — 그 표시가 바뀌면
            # 진짜 고장이 조용해집니다. 대신 줄 안에 무엇이 있었는지를
            # 남겨 다음 실행 로그가 답을 주게 합니다.
            log.warning(
                "%s / 강의목록: 강의 항목 %d개가 있는데 교안을 하나도 "
                "찾지 못했습니다(화면의 첨부 후보 %d개). 교안 없는 "
                "강의이거나, 첨부 링크 모양이 바뀐 것입니다.",
                course.name, items, files.candidates,
            )
            # 먼저 강의 줄 안의 버튼을 보여 줍니다. 화면 전체 덤프는
            # 앞쪽이 상단 메뉴로 다 차서 정작 이 줄들이 안 나옵니다.
            if rows_click is not None:
                total, sample = rows_click
                shown = "\n      ".join(
                    f"<{c.get('tag', '').lower()}"
                    + (f" onclick={c['onclick']!r}" if c.get("onclick") else "")
                    + (f" href={c['href']!r}" if c.get("href") else "")
                    + f"> {c.get('text', '')!r}"
                    for c in sample
                )
                log.warning("  강의 줄 안의 버튼 %d개:\n      %s", total, shown)
            # 읽는 법:
            #   fileDown=0, onclick=0   페이지가 안 열렸거나 다른 화면
            #   fileDown=0, onclick>0   선택자 문제 — onclick 샘플에서
            #                           진짜 함수 이름을 확인하세요
            #   fileDown>0, 본문        선택자는 맞고 다운로드가 실패
            #   fileDown>0, 프레임      프레임 순회 문제
            log.warning("  이 화면에 있는 것들:\n      %s",
                        self.describe_attachment_candidates())
        self.store.save()

    # -- 쪽지 --------------------------------------------------------
    # 강의자료가 쪽지로도 옵니다("[영어2] 한문단쓰기 자료 탑재 완료 알림").
    # 보낸이 칸은 `김진욱 (101394)` 처럼 이름과 사번이 함께 들어 있어
    # 익명화해서 저장합니다. 본문은 그대로 둡니다.
    def read_message_rows(self) -> list[dict[str, Any]]:
        rows = _eval_json(
            self.br.page,
            """() => {
                 const sel = __SEL__;
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const out = Array.from(document.querySelectorAll(sel)).map((tr, i) => {
                   const tds = Array.from(tr.querySelectorAll('td'));
                   if (tds.length < 6) return null;
                   const link = tr.querySelector('td.textLeft a');
                   return {
                     index: i,
                     no: squash(tds[1].innerText),
                     sender: squash(tds[2].innerText),
                     title: squash(link ? link.innerText : tds[3].innerText),
                     date: squash(tds[tds.length - 1].innerText),
                     has_file: !!tr.querySelector("a[onclick*='compressFile']")
                   };
                 }).filter(Boolean).filter(r => r.title);
                 return JSON.stringify(out);
               }""",
            sel=MESSAGE_ROW,
        )
        if not isinstance(rows, list):
            raise PageReadError(f"쪽지 목록이 리스트가 아닙니다: {type(rows).__name__}")
        return rows

    def message_page_count(self) -> int:
        n = self.board_page_count()
        return min(n, MAX_MESSAGE_PAGES)

    # -- 계정 레벨 메뉴 -------------------------------------------------
    # 과목이 아니라 계정에 딸린 메뉴(학습시간표·수강과목·학습도우미 공지/
    # 질의응답/행사안내/FAQ·커뮤니티·공개강좌). 현재 수집기는 과목 메뉴만
    # 봐서 이들을 통째로 놓칩니다. run() 에서 과목 루프 밖에서 1회 부릅니다.
    #
    # 개인정보(학번/이메일/전화/이름)는 마스킹하되 **메뉴·정보는 하나도
    # 빼지 않습니다**: 표가 있으면 표를, 없으면 본문 텍스트를 담고, 빈
    # 메뉴도 '(내용 없음)' 으로 남깁니다. 이름 열은 열 단위로, 그 밖의
    # 확실한 PII 토큰은 anon.mask_pii 로 가립니다. 제3자 자유텍스트 이름까지
    # 자동 검출하진 않으므로 결과물은 로컬 전용입니다.
    def _account_owner_name(self) -> str:
        """상단 프로필의 로그인 이름. 전역 마스킹에 씁니다."""
        for sel in ("li.w230 dl dt.fontB", "dt.fontB", ".photoInfo ~ dl dt"):
            try:
                loc = self.br.page.locator(sel).first
                if loc.count() and (t := loc.inner_text(timeout=800).strip()):
                    return t.split("(")[0].strip()
            except Exception:
                continue
        return ""

    def _account_menu_markdown(self, label: str) -> tuple[str, list[str]]:
        """현재 페이지(#bodyContent)를 '빠짐 없이' 마크다운으로. 이름 열 마스킹.

        돌려주는 두 번째 값은 이름 열에서 뽑은 '맨 이름' 들입니다 — .md 는
        여기서 가리지만, 같은 화면의 스크린샷(.png)에서도 그 문자열만 정확히
        가리라고 호출부에 넘겨 주려는 용도입니다.
        """
        page = self.br.page
        try:
            page.wait_for_load_state("domcontentloaded", timeout=self.br.timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(400)
        data = _eval_json(
            page,
            """() => {
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const root = document.querySelector('#bodyContent') || document.body;
                 const tables = Array.from(root.querySelectorAll('table')).map(t => ({
                   caption: squash((t.querySelector('caption') || {}).innerText),
                   heads: Array.from(t.querySelectorAll('thead th')).map(th => squash(th.innerText)),
                   rows: Array.from(t.querySelectorAll('tbody tr'))
                     .map(tr => Array.from(tr.querySelectorAll('th,td')).map(c => squash(c.innerText)))
                     .filter(r => r.some(Boolean)),
                 })).filter(t => t.rows.length);
                 const full = squash(root.innerText);
                 // 표 밖 텍스트(표는 구조화해 따로 담으므로 중복을 피해 제거).
                 const clone = root.cloneNode(true);
                 clone.querySelectorAll('table').forEach(t => t.remove());
                 const text_outside = squash(clone.innerText);
                 const blocked = /Web firewall|웹 방화벽|firewall security/.test(
                   document.body.innerHTML);
                 const relogin = /다시 로그인|처리 중 오류/.test(full);
                 return JSON.stringify({ tables, text: text_outside, blocked, relogin });
               }""",
        )
        if not isinstance(data, dict):
            return f"## {label}\n\n(읽기 실패)\n", []
        if data.get("blocked"):
            return f"## {label}\n\n(방화벽 차단 — 수집 안 함)\n", []
        if data.get("relogin"):
            return f"## {label}\n\n(세션 필요 — 건너뜀)\n", []

        lines = [f"## {label}", ""]
        names: list[str] = []
        tables = data.get("tables") or []
        for t in tables:
            cap = t.get("caption") or ""
            if cap:
                lines += [f"### {cap}", ""]
            heads = t.get("heads") or []
            name_cols = {i for i, h in enumerate(heads) if h in anon.NAME_COLUMNS}
            if heads:
                lines.append("| " + " | ".join(heads) + " |")
                lines.append("|" + "---|" * len(heads))
            for r in t["rows"]:
                cells = ["<NAME>" if i in name_cols else c.replace("|", "\\|")
                         for i, c in enumerate(r)]
                lines.append("| " + " | ".join(cells) + " |")
                # 이름 열의 '맨 이름' 은 스크린샷용으로 모읍니다(글자 2자 이상).
                for i in name_cols:
                    v = (r[i] if i < len(r) else "").strip()
                    if len(v) >= 2 and any(ch.isalpha() for ch in v):
                        names.append(v)
            lines.append("")
        # 표 밖 텍스트(문의처·안내문 등)도 빠뜨리지 않습니다.
        txt = (data.get("text") or "").strip()
        if txt:
            lines += [txt, ""]
        elif not tables:
            lines += ["(내용 없음)", ""]
        return "\n".join(lines), names

    def crawl_account(self) -> None:
        out_dir = self.layout.menu_dir(self.term, "_계정", "계정정보")
        out_dir.mkdir(parents=True, exist_ok=True)
        own = ""
        sections = []
        known_names: list[str] = []
        for label, tail in ACCOUNT_MENUS:
            try:
                self.br.goto(self.site.base_url.rstrip("/") + tail)
                if not own:
                    own = self._account_owner_name()
                md_text, names = self._account_menu_markdown(label)
                sections.append(md_text)
                known_names.extend(names)
            except Exception as exc:
                sections.append(f"## {label}\n\n(수집 실패: {type(exc).__name__})\n")
                self.stats.errors.append(f"계정/{label}: {exc}")
        # 이름 열에서 뽑은 이름을 레지스트리에 모아, 표 밖 자유 본문에 같은
        # 이름이 '맨 이름' 으로 나와도(.md·스크린샷·HTML) 가립니다.
        self._note_names(known_names)
        body = ("# 계정 정보 (계정 레벨 · 과목 무관)\n\n"
                "<!-- 개인정보(학번/이메일/전화/이름)는 마스킹됨(쪽지와 같은 수준). -->\n\n"
                + "\n".join(sections)).strip()
        body = anon.mask_pii(body, own_names=self._mask_names(own))
        digest = sha256_text(body)
        state_key = "account:info"
        if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
            self.stats.skipped += 1
        else:
            fp = out_dir / safe_name("계정정보.md")
            fp.write_text(body + "\n", encoding="utf-8")
            # .md 는 이름 열을 가렸지만, 스크린샷(마지막 메뉴 화면)에도 그
            # '맨 이름' 들이 남지 않게 정확 일치로 넘깁니다.
            self._snapshot(fp, known_names=known_names)
            self.store.mark_downloaded(state_key, path=str(fp), digest=digest)
            self.stats.posts += 1
        log.info("계정: 메뉴 %d개 수집(계정정보.md, 개인정보 마스킹)", len(ACCOUNT_MENUS))
        self.store.save()

    def crawl_messages(self, box: str = "RECEIVE") -> None:
        """쪽지함 하나를 끝까지 긁습니다."""
        out_dir = self.layout.menu_dir(self.term, "_쪽지", MENU_LABELS["message"])
        out_dir.mkdir(parents=True, exist_ok=True)
        total = 0

        self.br.goto(self.site.messages(box))
        pages = self.message_page_count()
        for page_no in range(1, pages + 1):
            if page_no > 1:
                # 게시판과 달리 페이지 이동이 평범한 GET 입니다.
                self.br.goto(self.site.messages(box, page_no))
            self.stats.pages += 1
            try:
                rows = self.read_message_rows()
            except PageReadError as exc:
                msg = f"쪽지({box}) {page_no}페이지: {exc}"
                log.error("%s", msg)
                self.stats.errors.append(msg)
                return
            if not rows:
                break

            list_url = self.br.page.url
            self._note_names(r.get("sender", "") for r in rows)
            for row in rows:
                who = anon.sender(row["sender"])
                state_key = sha256_text(
                    f"message|{box}|{row['no']}|{row['title']}|{row['date']}|{who}"
                )
                if self.store.is_downloaded(state_key):
                    self.stats.skipped += 1
                    continue
                try:
                    self._open_message(row, who, out_dir, state_key, box)
                    total += 1
                except Exception as exc:
                    msg = f"쪽지 '{row['title']}': {exc}"
                    log.warning("%s", msg)
                    self.stats.errors.append(msg)
                finally:
                    try:
                        self.br.goto(list_url)
                    except Exception:
                        pass
            self.store.save()

        log.info("쪽지(%s): %d건 저장", box, total)

    def _enter_detail(self, link, what: str) -> bool:
        """목록 행을 눌러 상세로 들어갑니다. 들어갔는지를 돌려줍니다.

        이 LMS 는 글 열람을 JS 폼 POST 로 합니다. 클릭 직후에는 **아직
        이전 페이지가** domcontentloaded 상태라, wait_for_load_state 는
        그 자리에서 즉시 돌아옵니다. 그래서 300ms 뒤에 옛 목록 화면을
        읽고, 거기서 첨부를 못 찾습니다.

        2026-11-13 실행의 진단 덤프가 그 증거입니다. 상세라면 본문과
        목록 버튼과 첨부가 있어야 하는데, 화면에 onclick 을 가진 요소가
        홈으로 가는 링크 하나뿐이었습니다 — 상세로 못 들어간 것입니다.
        그 결과가 "첨부 링크를 못 찾았습니다" 26건이었고, 원인과 전혀
        다른 곳을 가리키고 있었습니다.

        학습목차에서 같은 버그를 expect_navigation 으로 이미 고쳤고,
        16주차가 전부 저장되기 시작했습니다. 같은 처방입니다.

        navigation 을 못 보면 False 입니다. 호출부는 그걸 "상세 진입
        실패" 로 적어야 합니다 — "첨부를 못 찾았다" 로 적으면 다음
        사람이 또 선택자를 들여다봅니다.
        """
        page = self.br.page
        try:
            with page.expect_navigation(wait_until="domcontentloaded",
                                        timeout=self.br.timeout_ms):
                link.click(timeout=self.br.timeout_ms)
        except Exception as exc:
            log.warning("상세 진입 실패(%s): navigation 이 없었습니다 — %s",
                        what, exc)
            # AJAX 로 본문만 갈아끼우는 화면일 수도 있으니 한 번 더 봅니다.
            try:
                page.wait_for_load_state("domcontentloaded",
                                         timeout=self.br.timeout_ms)
            except Exception:
                pass
            page.wait_for_timeout(300)
            return False
        # 폼 POST 뒤에 화면이 다시 그려질 여유.
        page.wait_for_timeout(300)
        return True

    def _open_message(self, row: dict[str, Any], who: str, out_dir: Path,
                      state_key: str, box: str) -> None:
        page = self.br.page
        link = page.locator(MESSAGE_ROW).nth(row["index"]).locator(MESSAGE_LINK).first
        if link.count() == 0:
            raise RuntimeError("제목 링크를 찾지 못했습니다")
        entered = self._enter_detail(link, f"쪽지({box})/{row['title']}")

        body = ""
        for sel in (*DETAIL_BODY, "div#listBox"):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.inner_text().strip():
                    body = loc.inner_text()
                    break
            except Exception:
                continue
        body = re.sub(r"\n{3,}", "\n\n", (body or "").strip())

        stem = f"{row['no']}_{safe_name(row['title'], max_len=70)}" if row["no"] else \
               safe_name(row["title"], max_len=70)
        md = self._pending_path(state_key, out_dir, stem)
        md.write_text(
            f"# {row['title']}\n\n"
            f"- 쪽지함: {box}\n"
            f"- 보낸이: {who}\n"
            f"- 받은 날짜: {row.get('date','')}\n"
            f"- 수집시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n\n---\n\n"
            f"{body}\n",
            encoding="utf-8",
        )
        self.stats.posts += 1
        att = self._download_attachments(out_dir / safe_name(stem, max_len=60))
        # 보낸이를 .md 와 같은 이름표로 바꾼 뒤에 찍습니다. 화면 글자를
        # 고치므로 첨부를 다 받은 뒤에 합니다.
        self._snapshot(md, senders=[row.get("sender", "")])
        self._finish_post(
            state_key, md, att,
            expected_file=bool(row.get("has_file")),
            what=f"쪽지({box})/{row['title']}", entered=entered,
            extra={"title": row["title"], "sender": who},
        )

    # -- 게시판 ------------------------------------------------------
    def read_board_rows(self) -> list[dict[str, Any]]:
        """목록 테이블의 각 행을 읽습니다. 인덱스는 클릭할 때 씁니다."""
        rows = _eval_json(
            self.br.page,
            """() => {
                 const arg = __ARG__;
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const out = Array.from(document.querySelectorAll(arg.row))
                   .map((tr, i) => {
                     const tds = Array.from(tr.querySelectorAll('td'));
                     if (!tds.length) return null;
                     const link = tr.querySelector('a');
                     const text = tds.map(td => squash(td.innerText));
                     return {
                       index: i,
                       no: text[0] || '',
                       title: squash(link ? link.innerText : (text[1] || '')),
                       has_file: !!tr.querySelector(arg.icon),
                       author: text.length > 3 ? text[text.length - 3] : '',
                       date: text.length > 2 ? text[text.length - 2] : '',
                       cells: text
                     };
                   }).filter(Boolean).filter(r => r.title);
                 return JSON.stringify(out);
               }""",
            arg={"row": BOARD_ROW, "icon": FILE_ICON},
        )
        if not isinstance(rows, list):
            raise PageReadError(f"게시판 목록이 리스트가 아닙니다: {type(rows).__name__}")
        return rows

    def board_page_count(self) -> int:
        """게시판의 전체 페이지 수. 읽지 못하면 1 로 봅니다."""
        try:
            loc = self.br.page.locator(PAGINATOR_PAGES).first
            if loc.count() == 0:
                return 1
            match = re.search(r"(\d+)", loc.inner_text(timeout=3000) or "")
            return max(1, int(match.group(1))) if match else 1
        except Exception as exc:
            log.debug("페이지 수를 읽지 못했습니다: %s", exc)
            return 1

    def goto_board_page(self, target: int) -> bool:
        """게시판 target 페이지로 이동합니다.

        이 LMS 의 페이지 이동도 JS 라서 URL 로는 갈 수 없습니다. paginator
        안에서 그 숫자를 가진 요소를 눌러 보고, 목록이 실제로 바뀌었는지
        확인합니다. 바뀌지 않았으면 실패로 봅니다 — '클릭은 됐는데 아무
        일도 안 일어났다' 를 성공으로 처리하면 같은 페이지를 반복해서
        긁게 됩니다.
        """
        page = self.br.page
        before = self._page_signature()

        selectors = (
            f"{PAGINATOR} a",
            f"{PAGINATOR} span",
            f"{PAGINATOR} td",
        )
        for sel in selectors:
            loc = page.locator(sel)
            try:
                count = loc.count()
            except Exception:
                continue
            for i in range(min(count, 200)):
                item = loc.nth(i)
                try:
                    if (item.inner_text(timeout=1000) or "").strip() != str(target):
                        continue
                    item.click(timeout=self.br.timeout_ms)
                    page.wait_for_load_state("domcontentloaded", timeout=self.br.timeout_ms)
                    page.wait_for_timeout(400)
                except Exception:
                    continue
                if self._page_signature() != before:
                    return True
        return False

    def _page_signature(self) -> str:
        """현재 목록의 지문. 페이지가 실제로 바뀌었는지 판단하는 데 씁니다.

        여기서는 못 읽어도 예외를 올리지 않습니다 — 지문이 같으면
        '안 넘어갔다' 로 보고 goto_board_page 가 알아서 실패 처리합니다.
        """
        try:
            rows = self.read_board_rows()
        except PageReadError:
            return ""
        return "|".join(f"{r.get('no')}:{r.get('title')}" for r in rows[:5])

    def crawl_board(self, course: Course, key: str, *, titles_only: bool = False) -> None:
        """게시판 하나를 전부 긁습니다. 페이지가 여러 장이면 끝까지 넘깁니다."""
        label = MENU_LABELS.get(key, key)
        self.stats.boards_seen += 1
        total_pages = self.board_page_count()
        if total_pages > MAX_BOARD_PAGES:
            log.warning("%s / %s: 페이지가 %d장입니다. 앞 %d장만 봅니다.",
                        course.name, label, total_pages, MAX_BOARD_PAGES)
            total_pages = MAX_BOARD_PAGES

        list_url = self.br.page.url
        out_dir = self._dir(course, key)
        seen_titles: list[str] = []
        grand_total = 0

        for page_no in range(1, total_pages + 1):
            if page_no > 1 and not self.goto_board_page(page_no):
                # 여기서 조용히 넘어가면 사용자는 전부 받은 줄 압니다.
                msg = (f"{course.name}/{label}: {page_no}페이지로 넘어가지 못했습니다. "
                       f"전체 {total_pages}페이지 중 {page_no - 1}페이지까지만 수집했습니다. "
                       "페이지 이동 방식이 바뀐 것 같습니다(probe 로 확인하세요).")
                log.error(msg)
                self.stats.errors.append(msg)
                break

            try:
                rows = self.read_board_rows()
            except PageReadError as exc:
                # '글이 0건' 과 '못 읽었다' 는 전혀 다른 일입니다.
                # 섞어 버리면 27곳이 전부 비었는데도 정상으로 보입니다.
                msg = f"{course.name}/{label}: {exc}"
                log.error("%s", msg)
                self.stats.errors.append(msg)
                break
            if not rows:
                if page_no == 1:
                    self.stats.boards_empty += 1
                    log.info("%s / %s: 글이 없습니다.", course.name, label)
                break

            # 이 페이지 작성자 이름을 먼저 레지스트리에 모읍니다 — 상세 본문에
            # '맨 이름' 으로 다시 나와도 가릴 수 있게.
            self._note_names(r.get("author", "") for r in rows)
            grand_total += len(rows)
            if total_pages > 1:
                log.info("%s / %s: %d/%d페이지, %d건",
                         course.name, label, page_no, total_pages, len(rows))
            else:
                log.info("%s / %s: %d건", course.name, label, len(rows))

            if titles_only:
                seen_titles.extend(
                    f"- [{r['no']}] {r['title']}  ({r.get('date','')})" for r in rows
                )
                continue

            page_url = self.br.page.url
            for row in rows:
                state_key = sha256_text(
                    f"{course.course_id}|{key}|{row['no']}|{row['title']}|{row.get('date','')}"
                )
                if self.store.is_downloaded(state_key):
                    self.stats.skipped += 1
                    continue
                try:
                    self._open_post(course, key, row, out_dir, state_key)
                except Exception as exc:
                    msg = f"{course.name}/{label}/{row['title']}: {exc}"
                    log.warning("글 열기 실패: %s", msg)
                    self.stats.errors.append(msg)
                finally:
                    # 목록으로 복귀. 상세에서 뒤로가기가 막힌 화면이 있어
                    # URL 로 돌아갑니다. 단, 2페이지 이상에서는 URL 이
                    # 1페이지를 가리키므로 다시 그 페이지로 넘어가야 합니다.
                    try:
                        self.br.goto(page_url)
                        if page_no > 1 and not self.goto_board_page(page_no):
                            raise RuntimeError("목록 페이지 복귀 실패")
                    except Exception as exc:
                        log.error("%s/%s: %d페이지로 돌아가지 못해 중단합니다: %s",
                                  course.name, label, page_no, exc)
                        self.stats.errors.append(
                            f"{course.name}/{label}: {page_no}페이지 복귀 실패"
                        )
                        return

        if titles_only and seen_titles:
            body = (f"# {course.name} - {label} (목록만)\n\n"
                    "> 상세 페이지 진입은 LMS 에 기록이 남을 수 있어 하지 않았습니다.\n\n"
                    + "\n".join(seen_titles) + "\n")
            listing = out_dir / safe_name(f"{label}_목록.md")
            listing.write_text(body, encoding="utf-8")
            # 목록 화면만 찍습니다(상세에는 들어가지 않으므로). 여러 장이면
            # 마지막 장입니다. 목록 화면의 작성자 열 '맨 이름' 은 마지막 장
            # rows 에서 알고 있으니 그 문자열만 그림에서 가립니다.
            self._snapshot(listing, known_names=[r.get("author", "") for r in rows])
            self.stats.posts += 1

        if total_pages > 1:
            log.info("%s / %s: 총 %d건 (%d페이지)",
                     course.name, label, grand_total, total_pages)

        try:
            self.br.goto(list_url)
        except Exception:
            pass

    def _pending_path(self, state_key: str, out_dir: Path, stem: str) -> Path:
        """미완료로 남겨 둔 글의 저장 경로. 재시도 때 그 파일에 덮어씁니다.

        기억하지 않으면 unique_path 가 실행할 때마다 '제목_2.md',
        '제목_3.md' 를 만듭니다.
        """
        remembered = self.store.data["meta"].get(f"pending:{state_key}")
        if remembered:
            path = Path(remembered)
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        return unique_path(out_dir / f"{safe_name(stem)}.md")

    def _finish_post(self, state_key: str, md: Path, att: Attachments,
                     *, expected_file: bool, what: str, extra: dict[str, Any],
                     entered: bool = True) -> None:
        """첨부까지 성공했을 때만 '완료' 로 기록합니다.

        첨부가 하나라도 실패했는데 글을 완료로 적으면, 다음 실행이
        그 글을 건너뛰어 실패한 첨부를 영영 다시 받지 않습니다.
        """
        self.stats.entries += 1
        self.stats.files_known += att.known
        missing = list(att.failed)
        # att.known 을 빼면 안 됩니다. 내용이 같은 첨부는 두 번째부터
        # saved 에서 빠지므로, 미완료로 남아 재시도되는 글은 매번
        # "링크를 찾지 못했습니다" 가 되어 **영원히** 완료되지 않습니다.
        # (같은 파일이 강의목록에도 걸려 있으면 첫 실행에도 납니다.)
        got_something = att.saved or att.known
        if expected_file and not got_something and not missing:
            # 원인을 두 갈래로 갈라 적습니다. 섞어 놓으면 다음 사람이
            # 선택자만 들여다보다 하루를 씁니다.
            missing.append(
                "상세 화면에 못 들어갔습니다(클릭 후 navigation 없음)"
                if not entered else
                "목록에는 첨부 표시가 있는데 링크를 찾지 못했습니다"
            )
            # 선택자를 또 찍어 맞히지 않도록, 이 화면에 실제로 있는
            # 것들을 남깁니다. 다음 실행 로그가 답을 줍니다.
            log.error("  이 화면에 있는 것들:\n      %s",
                      self.describe_attachment_candidates())
        if missing:
            self.store.set_meta(f"pending:{state_key}", str(md))
            self.store.save()
            msg = f"{what}: 첨부 {len(missing)}건을 받지 못해 미완료로 둡니다 ({', '.join(missing[:3])})"
            log.error("%s", msg)
            self.stats.errors.append(msg)
            return
        self.store.forget("meta", f"pending:{state_key}")
        self.store.mark_downloaded(state_key, path=str(md), extra=extra)
        self.store.save()

    def _open_post(self, course: Course, key: str, row: dict[str, Any],
                   out_dir: Path, state_key: str) -> None:
        """목록 행을 클릭해 상세로 들어가 본문과 첨부를 저장합니다."""
        link = self.br.page.locator(BOARD_ROW).nth(row["index"]).locator(BOARD_TITLE_LINK).first
        if link.count() == 0:
            raise RuntimeError("제목 링크를 찾지 못했습니다")

        what = f"{course.name}/{MENU_LABELS.get(key, key)}/{row['title']}"
        entered = self._enter_detail(link, what)

        body = ""
        for sel in DETAIL_BODY:
            try:
                loc = self.br.page.locator(sel).first
                if loc.count() > 0:
                    body = loc.inner_text()
                    if body.strip():
                        break
            except Exception:
                continue
        body = re.sub(r"\n{3,}", "\n\n", (body or "").strip())

        title = safe_name(row["title"], max_len=80)
        stem = f"{row['no']}_{title}" if row["no"] else title
        post_dir = out_dir
        md = self._pending_path(state_key, post_dir, stem)
        # 개인정보 마스킹: 작성자 칸은 구조상 사람 이름이라 통째로(mask_name),
        # 제목·본문은 '확실한 PII 토큰'(이름(사번)·학번·이메일·전화·본인 이름)만
        # (mask_pii) 가립니다. 내용은 그대로 둬 '빠지는 정보' 가 없게 합니다.
        own = self._owner_name()
        self._note_names([row.get("author", "")])
        # 본문·제목은 '확실한 토큰'(mask_pii) 에 더해, 구조적으로 아는 이름
        # (작성자 등)도 own_names 자리로 넘겨 '맨 이름' 까지 가립니다.
        mask_names = self._mask_names(own)
        md.write_text(
            f"# {anon.mask_pii(row['title'], mask_names)}\n\n"
            f"- 과목: {course.name}\n"
            f"- 게시판: {MENU_LABELS.get(key, key)}\n"
            f"- 작성자: {anon.mask_name(row.get('author', ''), mask_names)}\n"
            f"- 작성일: {row.get('date','')}\n"
            f"- 수집시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n\n---\n\n"
            f"{anon.mask_pii(body, mask_names)}\n",
            encoding="utf-8",
        )
        # 작성자 '맨 이름' 은 정규식으론 못 잡지만 목록 행에서 구조적으로
        # 알고 있으니, 그 문자열만 그림에서도 가립니다(.md 와 같은 기준).
        self._snapshot(md, known_names=[row.get("author", "")])
        self.stats.posts += 1

        att = self._download_attachments(post_dir / safe_name(stem, max_len=60))
        self._finish_post(
            state_key, md, att,
            expected_file=bool(row.get("has_file")),
            what=what, entered=entered,
            extra={"title": row["title"], "attachments": [f.name for f in att.saved]},
        )

    def describe_attachment_candidates(self, limit: int = 12) -> str:
        """첨부를 못 찾았을 때, 화면에 **무엇이 있는지** 적어 둡니다.

        2026-11-12 실행에서 게시글 27건이 "목록에는 첨부 표시가 있는데
        링크를 찾지 못했습니다" 로 끝났습니다. 선택자를 또 찍어 맞히는
        대신, 그 화면에 실제로 있는 누를 만한 것들을 로그에 남깁니다.
        다음 실행 로그가 무엇을 맞춰야 하는지 알려 줍니다.
        """
        try:
            found = _eval_json(
                self.br.page,
                """() => {
                     const out = [];
                     const seen = new Set();
                     const add = (el, where) => {
                       const oc = el.getAttribute('onclick') || '';
                       const hr = el.getAttribute('href') || '';
                       const key = el.tagName + '|' + oc.slice(0, 40) + '|' + hr.slice(0, 40);
                       if (seen.has(key)) return;
                       seen.add(key);
                       out.push({
                         tag: el.tagName,
                         cls: (el.getAttribute('class') || '').slice(0, 60),
                         onclick: oc.slice(0, 120),
                         href: hr.slice(0, 120),
                         text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40),
                         where: where
                       });
                     };
                     for (const el of document.querySelectorAll('[onclick]')) add(el, 'onclick');
                     for (const el of document.querySelectorAll('a[href]')) {
                       const h = el.getAttribute('href') || '';
                       if (h && h !== '#' && !h.startsWith('javascript:void')) add(el, 'href');
                     }
                     return JSON.stringify(out.slice(0, __LIMIT__));
                   }""",
                limit=limit,
            )
        except PageReadError as exc:
            return f"{self._describe_frames()}\n      (화면을 읽지 못했습니다: {exc})"
        head = self._describe_frames()
        if not found:
            return (f"{head}\n      (누를 만한 것이 하나도 없습니다 — "
                    "상세 화면이 안 열렸을 수 있습니다)")
        lines = [head]
        for item in found:
            bits = [f"<{item['tag'].lower()}"]
            if item["cls"]:
                bits.append(f" class={item['cls']!r}")
            if item["onclick"]:
                bits.append(f" onclick={item['onclick']!r}")
            elif item["href"]:
                bits.append(f" href={item['href']!r}")
            bits.append(f"> {item['text']!r}")
            lines.append("".join(bits))
        return "\n      ".join(lines)

    def _describe_frames(self) -> str:
        """프레임 구성을 한 줄로. '어느 문서를 보고 있는가' 가 먼저입니다.

        _attachment_locators 는 메인 문서와 프레임을 전부 뒤지지만,
        0개로 끝났을 때 그 0 이 "프레임이 없다" 인지 "프레임은 있는데
        그 안에도 없다" 인지가 로그에 안 남았습니다. 둘은 고칠 곳이
        완전히 다릅니다.
        """
        page = self.br.page
        try:
            frames = list(page.frames)
        except Exception as exc:
            return f"[프레임 정보를 못 읽었습니다: {exc}]"
        bits = []
        for f in frames:
            try:
                n_fd = f.locator("[onclick*='fileDown']").count()
                n_any = f.locator("[onclick]").count()
                url = (f.url or "")[-60:]
            except Exception as exc:
                bits.append(f"{f.name or '이름없음'}=읽기실패({exc})")
                continue
            where = "본문" if f is page.main_frame else (f.name or "이름없음")
            bits.append(f"{where}(fileDown={n_fd}, onclick={n_any}, …{url})")
        return f"[현재 {page.url}]\n      [프레임 {len(frames)}개] " + " / ".join(bits)

    def _attachment_locators(self):
        """첨부 링크를 담은 locator 를 찾습니다. 프레임 안까지 봅니다.

        page.locator() 는 iframe 을 넘어가지 않습니다. 이 LMS 는 프레임을
        쓰므로(숨은 '파일다운로드프레임' 이 그 증거), 본문이 프레임 안에
        그려지는 화면에서는 메인 문서만 봐서는 첨부를 영영 못 찾습니다.
        기숙사 자동화 저장소가 같은 학교 포털에서 프레임을 전부 뒤지는
        방식을 쓰는 것도 같은 이유입니다.
        """
        page = self.br.page
        # 메인 문서 -> 프레임 순으로, 먼저 걸리는 것을 씁니다.
        contexts = [page, *[f for f in page.frames if f != page.main_frame]]
        for context in contexts:
            for sel in ATTACHMENT:
                try:
                    loc = context.locator(sel)
                    if loc.count() > 0:
                        where = "본문" if context is page else f"프레임({context.name or '이름없음'})"
                        log.debug("첨부 후보 %d개 발견 — %s / %s", loc.count(), where, sel)
                        return loc
                except Exception:
                    continue
        return None

    # 본문이 아니라 오류 페이지를 받아 놓고 성공으로 치지 않도록.
    _HTMLISH = ("text/html", "application/xhtml")

    def _fetch_filedown(self, call: filedown.Call, dest_dir: Path) -> Path | None:
        """fileDown(...) 링크를 클릭 대신 주소로 직접 받습니다.

        클릭하면 숨은 iframe 이 그 주소로 이동합니다. 서버가
        Content-Disposition 을 안 보내면 브라우저는 다운로드가 아니라
        '표시' 를 하고, 다운로드 이벤트는 오지 않습니다. 그러면 클릭은
        성공했는데 받은 파일은 없습니다. 주소를 직접 받으면 그 틈이
        없고, 받기 전에 파일 이름도 알 수 있습니다.
        """
        page = self.br.page
        target = filedown.url(page.url, call)
        name = safe_name(filedown.filename(call), max_len=120) or "첨부"

        # page.request 는 쿠키와 User-Agent 는 브라우저에서 물려받지만
        # **Referer 는 보내지 않습니다**(직접 확인: page.request 의 UA 는
        # HeadlessChrome/… 이고 Referer 는 None. "Playwright/x.y" UA 가
        # 나가는 것은 독립 컨텍스트 playwright.request.new_context() 쪽
        # 입니다 — 여기가 아닙니다. 그래서 UA 는 건드릴 필요가 없습니다).
        #
        # 다운로드 주소에 Referer 를 요구하는 서버는 흔하고(직링크 차단),
        # 그럴 때 돌아오는 것은 403 이 아니라 거부 안내 HTML 입니다.
        # 그러면 우리는 그걸 "세션이 끊겼다" 로 오진합니다.
        #
        # 로컬 픽스처에서는 아무 효과가 없고 실제 서버에서만 의미가
        # 있습니다. 틀려도 손해가 없는 쪽입니다.
        headers = {
            "Referer": page.url,
            "Accept": "*/*",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }

        reason = ""
        for attempt in (0, 1):
            try:
                resp = page.request.get(target, headers=headers,
                                        timeout=self.br.timeout_ms)
            except Exception as exc:
                reason = f"요청 실패: {exc}"
                break

            if resp.ok:
                ctype = (resp.headers.get("content-type") or "").lower()
                body = resp.body()
                if not body:
                    reason = "0바이트 응답"
                    break
                htmlish = (any(h in ctype for h in self._HTMLISH)
                           and not name.lower().endswith((".htm", ".html")))
                if not htmlish:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    out = unique_path(dest_dir / name)
                    out.write_bytes(body)
                    return out
                # 세션이 끊기면 서버가 파일 대신 로그인 화면(HTML)을 줍니다.
                # 그걸 .pdf 로 저장하면 사용자는 깨진 파일 14개를 받습니다.
                reason = f"파일 대신 HTML (content-type={ctype}, {len(body)}B)"
            else:
                reason = f"HTTP {resp.status}"

            # 401/403/HTML 은 세션 만료일 수 있습니다. 로그인 -> 게시판
            # 순회가 길어지면 중간에 세션이 떨어집니다. 딱 한 번 다시
            # 로그인하고 같은 주소를 다시 받아 봅니다.
            if attempt == 0 and (not resp.ok and resp.status in (401, 403)
                                 or resp.ok):
                log.warning("첨부 %s: %s — 세션을 다시 잡고 한 번만 재시도합니다.",
                            name, reason)
                try:
                    self.br.login(force=True)
                except Exception as exc:
                    reason = f"{reason} / 재로그인 실패: {exc}"
                    break
                continue
            break

        log.error("첨부를 받지 못했습니다(%s): %s", name, reason)
        self.stats.errors.append(f"첨부 실패: {name} — {reason}")
        return None

    def _download_attachments(self, dest_dir: Path) -> Attachments:
        """상세 화면의 첨부를 전부 받습니다.

        받은 것과 **못 받은 것** 을 함께 돌려줍니다. 못 받은 것을
        숨기면 호출부가 글을 '완료' 로 기록해 버리고, 다음 실행은
        그 글을 건너뜁니다.
        """
        out = Attachments()
        saved = out.saved
        own = self._owner_name()
        links = self._attachment_locators()
        if links is None:
            return out

        count = links.count()
        out.candidates = count
        for i in range(count):
            item = links.nth(i)
            try:
                text = _clean(item.inner_text())
            except Exception:
                text = ""
            if text in SKIP_LINK_TEXTS:
                continue

            try:
                onclick = item.get_attribute("onclick") or ""
                href = item.get_attribute("href") or ""
            except Exception:
                onclick = href = ""
            if _is_unsafe_click(onclick) or _is_unsafe_click(href):
                log.warning(
                    "첨부가 아니라 되돌릴 수 없는 동작으로 보여 건너뜁니다: %s (%s)",
                    text or f"#{i}", (onclick or href)[:80],
                )
                continue

            got: list[Path] = []
            label = text or f"#{i}"
            call = filedown.parse(onclick)
            if call:
                label = filedown.filename(call) or label
                one = self._fetch_filedown(call, dest_dir)
                if one is not None:
                    got = [one]
            else:
                # 주소를 알 수 없는 것만 누릅니다(쪽지 첨부는 DWR 로
                # zip 을 만든 뒤에야 주소가 정해집니다). 완료 판정은
                # '눌렸다' 가 아니라 '파일이 생겼다' 로 합니다.
                with self.br.capture_download(dest_dir) as captured:
                    try:
                        item.click()
                    except Exception as exc:
                        log.debug("첨부 클릭 실패(%s): %s", label, exc)
                got = list(captured)
            if not got:
                out.failed.append(label)

            for path in got:
                path = self._mask_attachment_name(path, own)
                digest = sha256_file(path)
                if self.store.is_downloaded(f"file:{digest}"):
                    path.unlink(missing_ok=True)
                    self.stats.skipped += 1
                    out.known += 1
                    continue
                self.store.mark_downloaded(f"file:{digest}", path=str(path), digest=digest)
                saved.append(path)
                self.stats.files += 1
        return out

    def _mask_attachment_name(self, path: Path, own: str) -> Path:
        """첨부 파일명 속 본인 학번·이름 등 확실한 PII 를 가린 이름으로 바꿉니다.

        내용(해시)은 그대로라 중복 판정에 영향이 없습니다. 바꿀 게 없으면
        원래 경로를, 바꾸다 실패하면도 원래 경로를 돌려줍니다(수집을 막지
        않습니다).
        """
        masked = anon.mask_filename(path.name, own_names=self._mask_names(own))
        if masked == path.name:
            return path
        target = unique_path(path.with_name(masked))
        try:
            path.rename(target)
            return target
        except Exception as exc:
            log.debug("첨부 파일명 마스킹 실패(%s): %s", path.name, exc)
            return path

    # -- 메인 루프 ---------------------------------------------------
    # -- 학습현황 --------------------------------------------------------
    # '학습 현황'(menu_00100)은 과목 전체 활동을 한 페이지에 모읍니다:
    # 활동 요약(과제/토론/시험/팀활동/출석/게시물/댓글) + 과제·토론·시험·
    # 팀활동·기타활동 '현황 리스트' 표. 과제 현황 표는 모든 과제의 제출여부·
    # 점수(취득/만점)·성적공개까지 한 줄씩 담아, 제출정보 상세의 요약판입니다.
    #
    # 성적·개인정보가 들어가므로 config 에서 SENSITIVE_MENUS 로 '기본 꺼짐 +
    # 업로드 제외'입니다. 켜면 로컬에만 저장됩니다. 본인 정보(학번/이메일/전화)
    # 표는 담지 않습니다.
    #
    # 과목 밖에서 이 URL 로 바로 가면 '다시 로그인' 스텁이 옵니다. crawl_course
    # 가 open_course 로 과목에 먼저 들어간 뒤 open_menu 로 오므로 정상입니다.
    def crawl_progress(self, course: Course) -> None:
        page = self.br.page
        # 표가 렌더될 때까지 기다립니다. 과목 세션이 없으면 '다시 로그인'
        # 안내만 떠서 표가 없습니다 — 그걸 '빈 현황' 으로 조용히 저장하지
        # 않고 건너뜁니다.
        try:
            page.wait_for_selector("#bodyContent table.boardListBasic",
                                   timeout=self.br.timeout_ms)
        except Exception:
            body = ""
            try:
                body = page.inner_text("#bodyContent")
            except Exception:
                pass
            if "다시 로그인" in body or "오류가 발생" in body:
                msg = f"{course.name}/학습현황: 과목 세션이 없어 표가 안 떴습니다(건너뜀)"
                log.warning("%s", msg)
                self.stats.errors.append(msg)
                return

        tables = _eval_json(
            page,
            """() => {
                 const squash = (v) => (v || '').replace(/\\s+/g, ' ').trim();
                 const root = document.querySelector('#bodyContent') || document.body;
                 const out = [];
                 for (const t of root.querySelectorAll(
                        'table.boardListBasic, table.boardListInfo')) {
                   const cap = squash((t.querySelector('caption') || {}).innerText);
                   const heads = Array.from(t.querySelectorAll('thead th'))
                       .map(th => squash(th.innerText));
                   const rows = Array.from(t.querySelectorAll('tbody tr'))
                       .map(tr => Array.from(tr.querySelectorAll('th,td'))
                               .map(c => squash(c.innerText)))
                       .filter(r => r.some(Boolean));
                   out.push({ caption: cap, heads: heads, rows: rows });
                 }
                 return JSON.stringify(out);
               }""",
        )
        if not isinstance(tables, list):
            raise PageReadError(f"학습현황이 리스트가 아닙니다: {type(tables).__name__}")

        # 본인 정보(학번/이메일/전화)·과목 배너 표는 제외.
        skip = ("학생정보", "이수과목", "담당교수")
        lines = [f"# {course.name} - 학습현황", "",
                 "<!-- 성적·개인정보 포함. 로컬 전용(업로드 제외). -->", ""]
        kept = 0
        for t in tables:
            cap = t.get("caption") or ""
            if any(s in cap for s in skip) or not t.get("rows"):
                continue
            kept += 1
            lines += [f"## {cap or '(제목 없음)'}", ""]
            heads = t.get("heads") or []
            if heads:
                lines.append("| " + " | ".join(heads) + " |")
                lines.append("|" + "---|" * len(heads))
            for r in t["rows"]:
                lines.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
            lines.append("")

        if not kept:
            log.info("%s / 학습현황: 표가 없습니다(수집할 현황 없음).", course.name)
            return
        body = "\n".join(lines).strip()
        digest = sha256_text(body)
        state_key = f"page:{course.course_id}:progress"
        out_dir = self._dir(course, "progress")
        if self.store.data["downloaded"].get(state_key, {}).get("sha256") == digest:
            self.stats.skipped += 1
        else:
            md = out_dir / safe_name("학습현황.md")
            md.write_text(body + "\n", encoding="utf-8")
            self._snapshot(md)
            self.store.mark_downloaded(state_key, path=str(md), digest=digest)
            self.stats.posts += 1
        log.info("%s / 학습현황: 현황 표 %d개 저장", course.name, kept)
        self.store.save()

    def crawl_course(self, course: Course) -> None:
        log.info("─" * 50)
        log.info("과목: %s (%s)", course.name, course.course_id)
        if not self.open_course(course):
            return
        self.stats.courses += 1

        board_keys = {"notice", "qna", "materials"}
        for key in self.enabled_menus():
            label = MENU_LABELS.get(key, key)
            try:
                if not self.open_menu(course, key):
                    continue
                if key in RISKY_MENUS:
                    self.crawl_board(course, key, titles_only=True)
                elif key in board_keys:
                    self.crawl_board(course, key)
                elif key == "curriculum":
                    self.save_curriculum(course)
                elif key == "lecture_list":
                    self.crawl_lecture_list(course)
                elif key == "assignment":
                    self.crawl_assignments(course)
                elif key == "progress":
                    self.crawl_progress(course)
                else:
                    self.save_page_text(course, key)
            except Exception as exc:
                msg = f"{course.name}/{label}: {exc}"
                log.error("메뉴 처리 실패: %s", msg)
                self.stats.errors.append(msg)
            finally:
                self.store.save()

    def run(self, include: list[str], exclude: list[str]) -> Stats:
        courses = self.filter_courses(self.list_courses(), include, exclude)
        if not courses:
            log.warning("수집 대상 과목이 없습니다. config 의 eclass.courses 를 확인하세요.")
            return self.stats
        log.info("대상 과목 %d개: %s", len(courses), ", ".join(c.name for c in courses))
        for course in courses:
            self.crawl_course(course)
        # 쪽지는 과목에 딸린 것이 아니라 계정 단위라 루프 밖에서 한 번만.
        if self.menus.get("message"):
            for box in self.message_boxes:
                try:
                    self.crawl_messages(box)
                except Exception as exc:
                    msg = f"쪽지({box}): {exc}"
                    log.error("%s", msg)
                    self.stats.errors.append(msg)
        # 계정 레벨 메뉴(학습시간표·도우미 공지/질의응답·FAQ 등)도 계정 단위.
        if self.menus.get("account"):
            try:
                self.crawl_account()
            except Exception as exc:
                msg = f"계정 메뉴: {exc}"
                log.error("%s", msg)
                self.stats.errors.append(msg)
        self.store.save()
        return self.stats
