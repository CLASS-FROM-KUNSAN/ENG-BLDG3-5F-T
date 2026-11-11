"""특정 프로그램이 뜨면 자동으로 녹화를 시작합니다.

오탐이 생깁니다:
  Zoom 을 회의가 아니라 그냥 켜 두기만 해도 녹화가 시작됩니다.
  창 제목 조건을 함께 쓰면(예: Teams 의 경우 "회의 중") 정확도가
  올라가지만, 프로그램이 제목을 바꾸면 다시 놓칩니다.
  확실한 건 수동 실행입니다. 이건 보조 수단으로 쓰세요.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..logging_setup import get as get_logger

log = get_logger("recorder.watcher")


def _running_processes() -> set[str]:
    """실행 중인 프로세스 이름(소문자)."""
    try:
        import psutil  # type: ignore
    except ImportError:
        if os.name == "nt":
            import subprocess
            try:
                out = subprocess.run(
                    ["tasklist", "/fo", "csv", "/nh"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout
                return {line.split('","')[0].strip('"').lower()
                        for line in out.splitlines() if line.strip()}
            except Exception as exc:
                log.debug("tasklist 실패: %s", exc)
                return set()
        return set()

    names = set()
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name:
            names.add(name)
    return names


def _window_titles() -> list[str]:
    """열려 있는 창 제목. Windows 전용(pygetwindow 또는 ctypes)."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        titles: list[str] = []
        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def handle(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                titles.append(buf.value)
            return True

        EnumWindows(callback_type(handle), 0)
        return titles
    except Exception as exc:
        log.debug("창 제목 열거 실패: %s", exc)
        return []


@dataclass
class Watcher:
    processes: Iterable[str] = ()
    window_titles: Iterable[str] = ()
    interval_sec: float = 15.0
    stop_grace_sec: float = 60.0

    def __post_init__(self) -> None:
        self._procs = {p.lower() for p in self.processes if p}
        self._titles = [t.lower() for t in self.window_titles if t]

    @staticmethod
    def _sleep(seconds: float, stop_check: Callable[[], bool] | None) -> bool:
        """중단 신호를 확인하며 잡니다. 중단되면 False."""
        waited = 0.0
        while waited < seconds:
            if stop_check is not None and stop_check():
                return False
            step = min(0.5, seconds - waited)
            time.sleep(step)
            waited += step
        return True

    def matched(self) -> str | None:
        """감지되면 무엇이 감지됐는지 돌려줍니다."""
        if self._procs:
            running = _running_processes()
            hit = self._procs & running
            if hit:
                return sorted(hit)[0]
        if self._titles:
            for title in _window_titles():
                low = title.lower()
                for needle in self._titles:
                    if needle in low:
                        return title
        return None

    def watch(
        self,
        on_start: Callable[[str], None],
        *,
        stop_check: Callable[[], bool] | None = None,
        cooldown_sec: float = 30.0,
        min_session_sec: float = 10.0,
        max_fast_failures: int = 3,
    ) -> None:
        """감지되면 on_start(이름) 를 부릅니다. on_start 는 블로킹해도 됩니다.

        재시작 폭주 방지:
          on_start 가 돌아온 뒤 곧바로 다시 감지되면(Zoom 을 계속 켜 둔 상태)
          무한히 녹화를 재시작하게 됩니다. ffmpeg 가 바로 죽는 상황이라면
          빈 폴더를 초당 수백 개 만들어 디스크를 채웁니다.
          그래서 (1) 대상이 사라질 때까지 기다린 뒤 재무장하고,
          (2) 그래도 바로 재감지되면 cooldown_sec 만큼 쉬고,
          (3) min_session_sec 보다 짧게 끝난 세션이 연속 max_fast_failures
          번이면 설정 문제로 보고 감시를 중단합니다.
        """
        if not self._procs and not self._titles:
            log.error("감시 대상이 비어 있습니다. watch_processes 또는 watch_window_titles 를 채우세요.")
            return

        log.info("감시 시작: 프로세스 %s / 창제목 %s (%.0f초 간격)",
                 sorted(self._procs) or "-", self._titles or "-", self.interval_sec)

        fast_failures = 0

        while True:
            if stop_check is not None and stop_check():
                log.info("감시 중단")
                return

            hit = self.matched()
            if not hit:
                time.sleep(self.interval_sec)
                continue

            log.info("감지: %s -> 녹화 시작", hit)
            started = time.monotonic()
            try:
                on_start(hit)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("녹화 중 오류")
            session_sec = time.monotonic() - started

            if session_sec < min_session_sec:
                # 너무 빨리 끝났습니다 = 녹화가 제대로 시작조차 못 했습니다.
                # 이때 '대상이 사라질 때까지' 기다리면 Zoom 을 계속 켜 둔
                # 사용자는 영원히 멈춘 것처럼 보이고, 실패 횟수도 쌓이지
                # 않아 포기 로직이 영원히 발동하지 않습니다.
                # 그래서 실패한 경우에는 기다리지 않고 쿨다운만 하고 재시도합니다.
                fast_failures += 1
                log.warning(
                    "녹화가 %.1f초 만에 끝났습니다(%d/%d). 설정이나 장치에 문제가 있을 수 있습니다.",
                    session_sec, fast_failures, max_fast_failures,
                )
                if fast_failures >= max_fast_failures:
                    log.error(
                        "짧은 녹화가 연속 %d회라 감시를 중단합니다. "
                        "doctor 명령으로 원인을 확인하세요.",
                        fast_failures,
                    )
                    return
                if not self._sleep(cooldown_sec, stop_check):
                    return
                continue

            fast_failures = 0

            # 정상적으로 끝난 세션: 대상이 사라질 때까지 기다린 뒤 재무장합니다.
            # (한 번의 '등장' 에 한 번만 녹화)
            if self.matched():
                log.info("대상이 아직 실행 중입니다. 종료를 기다립니다.")
                self.wait_until_gone(stop_check=stop_check)

            if stop_check is not None and stop_check():
                return

            # 사라짐 감지가 불안정한 경우를 대비한 최소 휴식.
            if self.matched() and cooldown_sec > 0:
                log.info("대상이 여전히 감지돼 %.0f초 쉽니다.", cooldown_sec)
                if not self._sleep(cooldown_sec, stop_check):
                    return

    def wait_until_gone(self, *, stop_check: Callable[[], bool] | None = None,
                        max_wait_sec: float = 12 * 3600) -> None:
        """대상이 사라지고 유예 시간이 지날 때까지 기다립니다.

        max_wait_sec 은 안전장치입니다. 감지 조건이 항상 참이 되는 설정
        (예: 창 제목에 흔한 단어를 넣음)에서 영원히 멈추지 않게 합니다.
        """
        gone_since: float | None = None
        deadline = time.monotonic() + max_wait_sec
        while True:
            if stop_check is not None and stop_check():
                return
            if time.monotonic() > deadline:
                log.warning(
                    "대상이 %.0f시간 넘게 계속 감지돼 대기를 멈춥니다. "
                    "watch_processes / watch_window_titles 조건이 너무 넓지 않은지 확인하세요.",
                    max_wait_sec / 3600,
                )
                return
            if self.matched():
                gone_since = None
            else:
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= self.stop_grace_sec:
                    log.info("대상이 %.0f초간 보이지 않아 녹화를 종료합니다.", self.stop_grace_sec)
                    return
            time.sleep(min(5.0, self.interval_sec))
