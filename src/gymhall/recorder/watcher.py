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
from dataclasses import dataclass
from typing import Callable, Iterable

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
    ) -> None:
        """감지되면 on_start(이름) 를 부릅니다. on_start 는 블로킹해도 됩니다."""
        if not self._procs and not self._titles:
            log.error("감시 대상이 비어 있습니다. watch_processes 또는 watch_window_titles 를 채우세요.")
            return

        log.info("감시 시작: 프로세스 %s / 창제목 %s (%.0f초 간격)",
                 sorted(self._procs) or "-", self._titles or "-", self.interval_sec)
        was_active = False
        gone_since: float | None = None

        while True:
            if stop_check is not None and stop_check():
                log.info("감시 중단")
                return

            hit = self.matched()
            if hit and not was_active:
                log.info("감지: %s -> 녹화 시작", hit)
                was_active = True
                gone_since = None
                try:
                    on_start(hit)
                except Exception:
                    log.exception("녹화 중 오류")
                finally:
                    was_active = False
                continue

            if not hit and was_active:
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= self.stop_grace_sec:
                    was_active = False
                    gone_since = None

            time.sleep(self.interval_sec)

    def wait_until_gone(self, *, stop_check: Callable[[], bool] | None = None) -> None:
        """대상이 사라지고 유예 시간이 지날 때까지 기다립니다."""
        gone_since: float | None = None
        while True:
            if stop_check is not None and stop_check():
                return
            if self.matched():
                gone_since = None
            else:
                gone_since = gone_since or time.time()
                if time.time() - gone_since >= self.stop_grace_sec:
                    log.info("대상이 %.0f초간 보이지 않아 녹화를 종료합니다.", self.stop_grace_sec)
                    return
            time.sleep(min(5.0, self.interval_sec))
