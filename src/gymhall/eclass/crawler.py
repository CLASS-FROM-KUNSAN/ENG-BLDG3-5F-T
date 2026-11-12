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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get as get_logger
from ..paths import Layout, safe_name, unique_path
from ..state import Store, sha256_file, sha256_text
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
SKIP_LINK_TEXTS = frozenset({"목록", "수정", "삭제", "답글", "이전글", "다음글", "인쇄"})

# 상세 화면의 첨부 링크 후보
ATTACHMENT = (
    "a[onclick*='ownload']", "a[href*='ownload']",
    "ul.fileList a", "div.fileBox a", "td.file a", "a.fileDown",
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

    def line(self) -> str:
        base = (f"과목 {self.courses}개 / 페이지 {self.pages} / 글 {self.posts} / "
                f"첨부 {self.files} / 건너뜀 {self.skipped}")
        return base + (f" / 오류 {len(self.errors)}" if self.errors else "")


def _clean(text: str) -> str:
    return re.sub(r"[ \t]+", " ", html.unescape(text or "")).strip()


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
        max_file_mb: int = 0,
        allow_extensions: Iterable[str] = (),
    ) -> None:
        self.br = browser
        self.site = site
        self.layout = layout
        self.store = store
        self.term = term
        self.menus = dict(menus)
        self.risky = dict(risky or {})
        self.menu_id_overrides = dict(menu_id_overrides or {})
        self.max_file_bytes = int(max_file_mb) * 1024 * 1024 if max_file_mb else 0
        self.allow_extensions = {e.lower().lstrip(".") for e in allow_extensions}
        self.stats = Stats()

    # -- 메뉴 선택 ---------------------------------------------------
    def enabled_menus(self) -> list[str]:
        """위험 메뉴는 옵트인이 두 겹(전체 동의 + 개별)일 때만 켜집니다."""
        out = [k for k, v in self.menus.items() if v and k not in RISKY_MENUS]
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
                "`python -m gymhall eclass probe` 를 돌려 보세요."
            )
        return courses

    def filter_courses(self, courses: list[Course], include: list[str],
                       exclude: list[str]) -> list[Course]:
        def matches(course: Course, needles: list[str]) -> bool:
            hay = f"{course.name} {course.course_id}".lower()
            return any(str(n).strip().lower() in hay for n in needles if str(n).strip())

        if include:
            courses = [c for c in courses if matches(c, include)]
        if exclude:
            courses = [c for c in courses if not matches(c, exclude)]
        return courses

    # -- 진입 --------------------------------------------------------
    def open_course(self, course: Course) -> bool:
        url = (self.site.open_course_home(course.course_id)
               if course.open_type == "open" else self.site.course_home(course.course_id))
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
        log.info("%s: '%s' 메뉴가 없습니다(과목에 해당 메뉴 미개설).",
                 course.name, MENU_LABELS.get(key, key))
        return False

    # -- 저장 --------------------------------------------------------
    def _dir(self, course: Course, key: str) -> Path:
        d = self.layout.menu_dir(self.term, course.slug, MENU_LABELS.get(key, key))
        d.mkdir(parents=True, exist_ok=True)
        return d

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
        self.store.mark_downloaded(state_key, path=str(out), digest=digest)
        self.stats.posts += 1
        log.info("저장: %s / %s", course.name, label)
        return out

    def _allowed_file(self, path: Path) -> bool:
        if self.allow_extensions and path.suffix.lower().lstrip(".") not in self.allow_extensions:
            log.info("확장자 제외: %s", path.name)
            return False
        if self.max_file_bytes and path.stat().st_size > self.max_file_bytes:
            log.info("용량 초과로 업로드 대상에서 제외: %s", path.name)
        return True

    # -- 게시판 ------------------------------------------------------
    def read_board_rows(self) -> list[dict[str, Any]]:
        """목록 테이블의 각 행을 읽습니다. 인덱스는 클릭할 때 씁니다."""
        try:
            return self.br.page.evaluate(
                """(sel) => {
                     const rows = Array.from(document.querySelectorAll(sel));
                     return rows.map((tr, i) => {
                       const tds = Array.from(tr.querySelectorAll('td'));
                       if (!tds.length) return null;
                       const link = tr.querySelector('a');
                       const text = tds.map(td => (td.innerText||'').replace(/\\s+/g,' ').trim());
                       return {
                         index: i,
                         no: text[0] || '',
                         title: (link ? (link.innerText||'') : (text[1]||'')).replace(/\\s+/g,' ').trim(),
                         has_file: !!tr.querySelector('.icon-data-color, img[alt*="첨부"], a[href*="ile"]'),
                         author: text.length > 3 ? text[text.length-3] : '',
                         date: text.length > 2 ? text[text.length-2] : '',
                         cells: text
                       };
                     }).filter(Boolean).filter(r => r.title);
                   }""",
                BOARD_ROW,
            )
        except Exception as exc:
            log.warning("게시판 목록 읽기 실패: %s", exc)
            return []

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
        """현재 목록의 지문. 페이지가 실제로 바뀌었는지 판단하는 데 씁니다."""
        rows = self.read_board_rows()
        return "|".join(f"{r.get('no')}:{r.get('title')}" for r in rows[:5])

    def crawl_board(self, course: Course, key: str, *, titles_only: bool = False) -> None:
        """게시판 하나를 전부 긁습니다. 페이지가 여러 장이면 끝까지 넘깁니다."""
        label = MENU_LABELS.get(key, key)
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

            rows = self.read_board_rows()
            if not rows:
                if page_no == 1:
                    log.info("%s / %s: 글이 없습니다.", course.name, label)
                break

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
            (out_dir / safe_name(f"{label}_목록.md")).write_text(body, encoding="utf-8")
            self.stats.posts += 1

        if total_pages > 1:
            log.info("%s / %s: 총 %d건 (%d페이지)",
                     course.name, label, grand_total, total_pages)

        try:
            self.br.goto(list_url)
        except Exception:
            pass

    def _open_post(self, course: Course, key: str, row: dict[str, Any],
                   out_dir: Path, state_key: str) -> None:
        """목록 행을 클릭해 상세로 들어가 본문과 첨부를 저장합니다."""
        link = self.br.page.locator(BOARD_ROW).nth(row["index"]).locator(BOARD_TITLE_LINK).first
        if link.count() == 0:
            raise RuntimeError("제목 링크를 찾지 못했습니다")

        link.click()
        self.br.page.wait_for_load_state("domcontentloaded", timeout=self.br.timeout_ms)
        self.br.page.wait_for_timeout(300)

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
        md = unique_path(post_dir / f"{safe_name(stem)}.md")
        md.write_text(
            f"# {row['title']}\n\n"
            f"- 과목: {course.name}\n"
            f"- 게시판: {MENU_LABELS.get(key, key)}\n"
            f"- 작성자: {row.get('author','')}\n"
            f"- 작성일: {row.get('date','')}\n"
            f"- 수집시각: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S}\n\n---\n\n"
            f"{body}\n",
            encoding="utf-8",
        )
        self.stats.posts += 1

        files = self._download_attachments(post_dir / safe_name(stem, max_len=60))
        self.store.mark_downloaded(
            state_key, path=str(md),
            extra={"title": row["title"], "attachments": [f.name for f in files]},
        )
        self.store.save()

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

    def _download_attachments(self, dest_dir: Path) -> list[Path]:
        """상세 화면의 첨부를 전부 받습니다."""
        saved: list[Path] = []
        links = self._attachment_locators()
        if links is None:
            return saved

        count = links.count()
        for i in range(count):
            item = links.nth(i)
            try:
                text = _clean(item.inner_text())
            except Exception:
                text = ""
            if text in SKIP_LINK_TEXTS:
                continue
            with self.br.capture_download(dest_dir) as got:
                try:
                    item.click()
                except Exception as exc:
                    log.debug("첨부 클릭 실패(%s): %s", text or f"#{i}", exc)
            for path in got:
                digest = sha256_file(path)
                if self.store.is_downloaded(f"file:{digest}"):
                    path.unlink(missing_ok=True)
                    self.stats.skipped += 1
                    continue
                self.store.mark_downloaded(f"file:{digest}", path=str(path), digest=digest)
                self._allowed_file(path)
                saved.append(path)
                self.stats.files += 1
        return saved

    # -- 메인 루프 ---------------------------------------------------
    def crawl_course(self, course: Course) -> None:
        log.info("─" * 50)
        log.info("과목: %s (%s)", course.name, course.course_id)
        if not self.open_course(course):
            return
        self.stats.courses += 1

        board_keys = {"notice", "qna", "materials", "assignment"}
        for key in self.enabled_menus():
            label = MENU_LABELS.get(key, key)
            try:
                if not self.open_menu(course, key):
                    continue
                if key in RISKY_MENUS:
                    self.crawl_board(course, key, titles_only=True)
                elif key in board_keys:
                    self.crawl_board(course, key)
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
        self.store.save()
        return self.stats
