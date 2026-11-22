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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


class BrowserMissing(Exception):
    """Chromium 이 아예 안 받아져 있음(headless shell 도, 일반 chromium 도)."""


def _missing_executable(exc: BaseException) -> bool:
    return "Executable doesn't exist" in str(exc)


def launch_chromium(pw, *, headless: bool = True):
    """Chromium 을 띄웁니다. 창 없이 띄울 때 쓰는 headless shell 이 없으면 일반 Chromium 으로.

    강의실 PC 에서 `playwright install chromium` 이 학교망 인증서 문제로 일부만 받았습니다:
    일반 Chromium(chromium-1248)은 받았는데 chrome-headless-shell 만 실패해,
    수집이 'Executable doesn't exist …chromium_headless_shell-1248' 로 멈췄습니다.
    channel="chromium" 은 일반 Chromium 을 새 headless 모드로 띄웁니다(playwright 1.49+).
    """
    try:
        return pw.chromium.launch(headless=headless)
    except Exception as exc:
        if not (headless and _missing_executable(exc)):
            raise
        first = exc
    log.warning("Chrome Headless Shell 이 없어 일반 Chromium 으로 창 없이 띄웁니다. "
                "(scripts\\setup.bat 을 다시 실행하면 headless shell 도 받습니다)")
    try:
        return pw.chromium.launch(headless=True, channel="chromium")
    except Exception as exc:
        if _missing_executable(exc):
            raise BrowserMissing(
                "Playwright 용 Chromium 이 받아져 있지 않습니다.\n"
                "  scripts\\setup.bat 을 다시 실행하거나\n"
                "  <.venv 의 python> -m playwright install chromium\n"
                "  을 실행하세요. 학교망에서 'self-signed certificate' 로 실패하면\n"
                "  docs/TROUBLESHOOTING.md 의 'Playwright 브라우저 받기' 를 보세요."
            ) from first
        raise
#
# 실제 마크업(2026-11 기준, 개발자도구로 확인):
#   <form id="loginForm" action="/User.do" method="post" onsubmit="return false;">
#     <input type="hidden" name="cmd" value="loginUser">
#     <ul class="user_box">
#       <li><input id="id" name="userId" type="text" placeholder="아이디"></li>
#       <li><input id="pw" name="password" type="password"
#                  onkeypress="if(event.keyCode == 13){fnLogin()}"></li>
#     </ul>
#     <p><a href="#" onclick="fnLogin();" class="loginBtn">로그인</a></p>
#   </form>
#
# 주의할 점 두 가지:
#   1) id 속성은 `userId`/`password` 가 아니라 `id`/`pw` 입니다.
#      (name 속성과 id 속성이 서로 다릅니다)
#   2) onsubmit="return false" 라서 폼의 기본 제출은 막혀 있습니다.
#      그래도 Enter 는 동작합니다 — 비밀번호 칸의 onkeypress 가
#      직접 fnLogin() 을 부르고, fnLogin() 은 form.submit() 을 호출해
#      onsubmit 을 우회하기 때문입니다.
#
# 여러 후보를 두는 이유는 학교가 마크업을 조금 바꿔도 버티게 하기 위함입니다.
ID_SELECTORS = (
    "#loginForm input[name='userId']",
    "input[name='userId']",
    "#loginForm #id",
    "#loginForm input[type='text']",
)
PW_SELECTORS = (
    "#loginForm input[name='password']",
    "input[name='password']",
    "#loginForm #pw",
    "#loginForm input[type='password']",
)
SUBMIT_SELECTORS = (
    "#loginForm a.loginBtn",
    "#loginForm a[onclick*='fnLogin']",
    "a.loginBtn",
    "[onclick*='fnLogin']",
    "#loginForm button[type='submit']",
    "#loginForm input[type='submit']",
)
# 로그인 성공 판정.
# 하나만 보지 않습니다. 테마나 페이지 저장 도구에 따라 로그아웃 링크가
# 사라질 수 있어, '내 강의실 목록 select 가 있다' 는 조건도 함께 봅니다.
LOGGED_IN_SELECTORS = (
    "a[href*='cmd=logoutUser']",
    "select[name='courseDTO.courseId']",
    "a[onclick*='logoutUser']",
)
# 반대 증거: 비밀번호 입력란이 보이면 아직 로그인 전입니다.
LOGGED_OUT_SELECTOR = "#loginForm input[name='password'], input[name='password']"


# 로그인 실패나 중복 로그인에서 뜨는 대화상자의 내용으로 원인을 구분합니다.
DUPLICATE_LOGIN_HINTS = ("중복", "이미 로그인", "다른 곳", "동시 접속")
WRONG_CREDENTIAL_HINTS = ("비밀번호", "일치하지", "확인하", "잘못", "아이디")
LOCKED_HINTS = ("잠금", "잠겼", "차단", "정지", "5회", "횟수")


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
        # Playwright 는 기본적으로 alert/confirm 을 조용히 닫아 버립니다.
        # 그러면 "비밀번호가 일치하지 않습니다" 같은 실패 사유를 영영 못 봅니다.
        # 내용을 모아 두었다가 오류 메시지에 그대로 실어 줍니다.
        self.dialogs: list[str] = []

    # -- 수명주기 ----------------------------------------------------
    def __enter__(self) -> EclassBrowser:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:
            raise PlaywrightMissing() from exc

        self._pw = sync_playwright().start()
        self._browser = launch_chromium(self._pw, headless=self.headless)

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
        self._install_dialog_handler()
        return self

    def _install_dialog_handler(self) -> None:
        """alert / confirm 을 받아 기록하고 수락합니다.

        수락하는 이유: 이 LMS 는 중복 로그인 안내처럼 '확인' 을 눌러야
        다음으로 넘어가는 대화상자를 씁니다. 무시하면 그 자리에서 멈춥니다.
        """

        def on_dialog(dialog) -> None:
            message = (dialog.message or "").strip()
            if message:
                self.dialogs.append(message)
                log.info("대화상자: %s", message)
            try:
                dialog.accept()
            except Exception:
                try:
                    dialog.dismiss()
                except Exception:
                    pass

        self.page.on("dialog", on_dialog)

    def take_dialogs(self) -> list[str]:
        """모인 대화상자 내용을 꺼내고 비웁니다."""
        found, self.dialogs = list(self.dialogs), []
        return found

    def diagnose(self, messages: list[str]) -> str:
        """대화상자 내용으로 실패 원인을 짚어 줍니다."""
        joined = " ".join(messages)
        if any(h in joined for h in LOCKED_HINTS):
            return ("계정이 잠겼을 수 있습니다. 브라우저로 직접 로그인해 "
                    "상태를 확인하세요. 자동 재시도는 하지 않습니다.")
        if any(h in joined for h in DUPLICATE_LOGIN_HINTS):
            return ("다른 곳에서 이미 로그인된 상태입니다. "
                    "그 세션을 로그아웃하거나 잠시 뒤 다시 시도하세요.")
        if any(h in joined for h in WRONG_CREDENTIAL_HINTS):
            return ("아이디 또는 비밀번호가 맞지 않습니다. "
                    "python -m syncnote secrets set eclass 로 다시 저장하세요.")
        return ""

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
            raise LoginError("비밀번호가 없습니다. python -m syncnote secrets set eclass")

        id_input, id_sel = self._first_visible(ID_SELECTORS)
        pw_input, pw_sel = self._first_visible(PW_SELECTORS)
        if id_input is None or pw_input is None:
            raise LoginError(
                "로그인 입력란을 찾지 못했습니다. 학교가 페이지를 바꿨을 수 있습니다.\n"
                "  python -m syncnote eclass probe --no-headless\n"
                "로 실제 화면을 확인한 뒤 browser.py 의 선택자를 고치세요."
            )

        log.info("로그인 시도: %s (%s / %s)", self.username, id_sel, pw_sel)
        id_input.fill(self.username)
        pw_input.fill(password)

        self.take_dialogs()  # 이전 단계에서 남은 것 비우기
        submit, _ = self._first_visible(SUBMIT_SELECTORS)
        try:
            with self.page.expect_navigation(wait_until="domcontentloaded", timeout=self.timeout_ms):
                if submit is not None:
                    submit.click()
                else:
                    # 버튼을 못 찾으면 엔터로 제출합니다.
                    # 폼에 onsubmit="return false" 가 걸려 있어 기본 제출은
                    # 막혀 있지만, 비밀번호 칸의 onkeypress 가 fnLogin() 을
                    # 직접 부르기 때문에 동작합니다.
                    pw_input.press("Enter")
        except Exception as exc:
            log.debug("로그인 후 네비게이션 대기 실패(무시하고 상태를 확인합니다): %s", exc)

        # 중복 로그인 안내처럼 '확인' 을 눌러야 넘어가는 화면이 있습니다.
        self.page.wait_for_timeout(1200)
        self._click_confirm_if_present()
        self.page.wait_for_timeout(500)

        if not self.is_logged_in():
            messages = self.take_dialogs()
            detail = self.diagnose(messages)
            lines = ["로그인에 실패했습니다."]
            if messages:
                lines.append("사이트가 보여준 메시지: " + " / ".join(messages))
            if detail:
                lines.append(detail)
            else:
                lines.append("아이디와 비밀번호를 확인하세요.")
            lines.append(
                "비밀번호를 여러 번 틀리면 학교 계정이 잠길 수 있어 "
                "자동 재시도는 하지 않습니다."
            )
            raise LoginError("\n".join(lines))

        log.info("로그인 성공")
        self.save_state()

    def _click_confirm_if_present(self) -> bool:
        """'확인' 류 버튼이 보이면 누릅니다.

        중복 로그인 안내가 모달로 뜨면 누르지 않는 한 다음으로 넘어가지
        않습니다. 브라우저 기본 대화상자(alert/confirm)는 dialog 핸들러가
        처리하지만, 페이지가 직접 그린 모달은 여기서 눌러야 합니다.
        """
        labels = ("확인", "예", "OK", "계속")
        for label in labels:
            try:
                button = self.page.get_by_role("button", name=label, exact=True).first
                if button.count() > 0 and button.is_visible():
                    button.click(timeout=3000)
                    log.info("'%s' 버튼을 눌렀습니다(중복 로그인 안내 등).", label)
                    return True
            except Exception:
                continue
        return False

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
