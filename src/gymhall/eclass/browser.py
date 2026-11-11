"""Playwright 로 eClass 에 로그인하고 세션을 유지합니다.

왜 Playwright 인가:
  이 LMS 는 게시글 열람/첨부 다운로드를 JS 가 숨은 <form> 을 POST 하는
  방식으로 처리합니다. requests 로 하려면 13개짜리 hidden 필드를 전부
  역설계해야 하고, 학교가 필드 하나만 바꿔도 조용히 깨집니다.
  브라우저로 '클릭' 하면 사이트의 JS 가 알아서 처리합니다.

세션 저장:
  로그인 쿠키를 storage_state 로 저장해 다음 실행 때 재사용합니다.
  매번 로그인하면 학교 서버에 불필요한 부담이 가고, 비정상 접근으로
  탐지될 수 있습니다.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .. import secrets
from ..logging_setup import get as get_logger
from .urls import Site

log = get_logger("eclass.browser")


class LoginError(Exception):
    pass


class PlaywrightMissing(Exception):
    def __str__(self) -> str:  # pragma: no cover
        return (
            "Playwright 가 설치되지 않았습니다.\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium"
        )


# 로그인 폼 후보. 사이트가 바뀌어도 하나는 걸리도록 여러 개를 둡니다.
ID_SELECTORS = (
    "#loginForm input[name='userId']",
    "input[name='userId']",
    "#userId",
    "#loginForm input[type='text']",
)
PW_SELECTORS = (
    "#loginForm input[name='password']",
    "input[name='password']",
    "#password",
    "#loginForm input[type='password']",
)
SUBMIT_SELECTORS = (
    "#loginForm a[onclick*='fnLogin']",
    "#loginForm button[type='submit']",
    "#loginForm input[type='submit']",
    "a[onclick*='fnLogin']",
    "[onclick*='fnLogin']",
)
# 로그인 성공 판정.
# 하나만 보지 않습니다. 페이지 저장 도구나 테마 변경으로 로그아웃 링크가
# 사라질 수 있어, '내 강의실 목록 select 가 있다' 는 조건도 함께 봅니다.
LOGGED_IN_SELECTORS = (
    "a[href*='cmd=logoutUser']",
    "select[name='courseDTO.courseId']",
    "a[href*='cmd=logoutUser'], a[onclick*='logoutUser']",
)
# 반대 증거: 비밀번호 입력란이 보이면 아직 로그인 전입니다.
LOGGED_OUT_SELECTOR = "#loginForm input[name='password'], input[name='password']"


class EclassBrowser:
    """with 문으로 쓰세요. 나갈 때 브라우저를 정리합니다."""

    def __init__(
        self,
        site: Site,
        username: str,
        *,
        headless: bool = True,
        timeout_ms: int = 30000,
        request_delay_sec: float = 1.0,
        state_path: Path | None = None,
        download_dir: Path | None = None,
    ) -> None:
        self.site = site
        self.username = username
        self.headless = headless
        self.timeout_ms = int(timeout_ms)
        self.request_delay_sec = float(request_delay_sec)
        self.state_path = Path(state_path) if state_path else None
        self.download_dir = Path(download_dir) if download_dir else None
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    # -- 수명주기 ----------------------------------------------------
    def __enter__(self) -> "EclassBrowser":
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise PlaywrightMissing() from exc

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)

        kwargs: dict[str, Any] = {
            "accept_downloads": True,
            "locale": "ko-KR",
            "viewport": {"width": 1440, "height": 900},
        }
        if self.state_path and self.state_path.exists():
            try:
                json.loads(self.state_path.read_text(encoding="utf-8"))
                kwargs["storage_state"] = str(self.state_path)
                log.debug("저장된 세션을 불러옵니다: %s", self.state_path)
            except (json.JSONDecodeError, OSError):
                log.warning("저장된 세션이 손상돼 무시합니다.")

        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(self.timeout_ms)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        for obj, label in ((self._context, "context"), (self._browser, "browser")):
            try:
                if obj is not None:
                    obj.close()
            except Exception as exc:  # pragma: no cover
                log.debug("%s 종료 중 오류 무시: %s", label, exc)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass
        self._context = self._browser = self._pw = self.page = None

    # -- 이동 --------------------------------------------------------
    def goto(self, url: str, *, wait: str = "domcontentloaded") -> None:
        """서버를 배려해 요청 간 간격을 둡니다."""
        if self.request_delay_sec > 0:
            time.sleep(self.request_delay_sec)
        log.debug("이동: %s", url)
        self.page.goto(url, wait_until=wait, timeout=self.timeout_ms)

    def save_state(self) -> None:
        if self.state_path and self._context is not None:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self.state_path))
            log.debug("세션 저장: %s", self.state_path)

    # -- 로그인 ------------------------------------------------------
    def is_logged_in(self) -> bool:
        """긍정 증거가 하나라도 있고 비밀번호 입력란이 안 보이면 로그인 상태."""
        try:
            password_visible = False
            loc = self.page.locator(LOGGED_OUT_SELECTOR).first
            if loc.count() > 0:
                try:
                    password_visible = loc.is_visible()
                except Exception:
                    password_visible = False
            if password_visible:
                return False
            for sel in LOGGED_IN_SELECTORS:
                try:
                    if self.page.locator(sel).count() > 0:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _first_visible(self, selectors: tuple[str, ...]):
        for sel in selectors:
            loc = self.page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    return loc, sel
            except Exception:
                continue
        return None, None

    def login(self, password: str | None = None, *, force: bool = False) -> None:
        """저장된 세션이 살아 있으면 로그인을 건너뜁니다."""
        self.goto(self.site.home)

        if not force and self.is_logged_in():
            log.info("저장된 세션으로 이미 로그인된 상태입니다.")
            return

        if password is None:
            password = secrets.get("eclass")
        if not password:
            raise LoginError("비밀번호가 없습니다. python -m gymhall secrets set eclass")

        id_input, id_sel = self._first_visible(ID_SELECTORS)
        pw_input, pw_sel = self._first_visible(PW_SELECTORS)
        if id_input is None or pw_input is None:
            raise LoginError(
                "로그인 입력란을 찾지 못했습니다. 학교가 페이지를 바꿨을 수 있습니다.\n"
                "  python -m gymhall eclass probe --no-headless\n"
                "로 실제 화면을 확인한 뒤 browser.py 의 선택자를 고치세요."
            )

        log.info("로그인 시도: %s (%s / %s)", self.username, id_sel, pw_sel)
        id_input.fill(self.username)
        pw_input.fill(password)

        submit, _ = self._first_visible(SUBMIT_SELECTORS)
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=self.timeout_ms):
                if submit is not None:
                    submit.click()
                else:
                    # 버튼을 못 찾으면 엔터로 폼을 제출합니다.
                    pw_input.press("Enter")
        except Exception as exc:
            log.debug("로그인 후 네비게이션 대기 실패(무시하고 상태를 확인합니다): %s", exc)

        # alert() 로 막히는 경우가 있어 한 번 더 확인합니다.
        self.page.wait_for_timeout(1000)
        if not self.is_logged_in():
            raise LoginError(
                "로그인에 실패했습니다. 아이디/비밀번호를 확인하세요.\n"
                "비밀번호를 여러 번 틀리면 학교 계정이 잠길 수 있으니 "
                "자동 재시도는 하지 않습니다."
            )

        log.info("로그인 성공")
        self.save_state()

    # -- 다운로드 ----------------------------------------------------
    @contextmanager
    def capture_download(self, dest_dir: Path, timeout_ms: int | None = None) -> Iterator[list[Path]]:
        """with 블록 안에서 클릭하면 떨어지는 파일을 dest_dir 에 저장합니다.

        이 LMS 는 숨은 iframe(파일다운로드프레임)으로 파일을 내려보내는데,
        Playwright 의 download 이벤트가 그것도 잡아 줍니다.
        """
        from ..paths import safe_name, unique_path

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        try:
            with self.page.expect_download(timeout=timeout_ms or self.timeout_ms) as info:
                yield saved
            download = info.value
            suggested = download.suggested_filename or "download.bin"
            target = unique_path(dest_dir / safe_name(suggested))
            download.save_as(str(target))
            saved.append(target)
            log.info("받음: %s", target.name)
        except Exception as exc:
            log.warning("다운로드 실패 또는 시간 초과: %s", exc)
