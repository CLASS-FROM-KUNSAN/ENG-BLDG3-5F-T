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
        max_backoff_sec: float = 600.0,
    ) -> None:
        """감지되면 on_start(이름) 를 부릅니다. on_start 는 블로킹해도 됩니다.

        감시는 스스로 포기하지 않습니다(녹화를 막는 구조를 두지 않음):
          - on_start 가 곧바로 끝나면(녹화가 시작조차 못 함 — 다른 녹화가 잠금을 쥐고 있거나
            장치 문제) 쉬는 간격을 cooldown_sec 에서 두 배씩 늘려(최대 max_backoff_sec)
            다시 시도합니다. 연속 max_fast_failures 번이면 오류 로그를 한 번 크게 남깁니다.
            예전에는 3번이면 그날 감시를 끝내, 예약 녹화와 겹친 뒤의 수업을 놓쳤습니다.
          - 정상 녹화가 끝났는데(최대 녹화 시간에 걸림) 대상이 아직 떠 있으면 수업이 이어지는
            것이므로 곧바로 다음 녹화를 시작합니다. 예전에는 대상이 사라질 때까지 기다려
            나머지 수업을 녹화하지 않았습니다.
          - 대상이 사라졌다가 다시 뜨면 다시 녹화합니다(한 번의 '등장' 에 이어진 녹화).
          - 녹화가 폭주(초당 수백 번 재시작)하지 않는 것은 위의 늘어나는 간격이 보장합니다.
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
                if not self._sleep(self.interval_sec, stop_check):
                    return
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

            if stop_check is not None and stop_check():
                return

            if session_sec < min_session_sec:
                # 너무 빨리 끝났습니다 = 녹화가 제대로 시작조차 못 했습니다. 포기하지 않고
                # 간격을 늘려 가며 다시 시도합니다(다른 녹화가 끝나면 이어서 녹화).
                fast_failures += 1
                wait = max(self.interval_sec,
                           min(max_backoff_sec, cooldown_sec * (2 ** (fast_failures - 1))))
                log.warning(
                    "녹화가 %.1f초 만에 끝났습니다(연속 %d번). %.0f초 뒤 다시 시도합니다.",
                    session_sec, fast_failures, wait,
                )
                if fast_failures == max_fast_failures:
                    log.error(
                        "짧은 녹화가 연속 %d번입니다. 감시는 계속하지만 설정이나 장치를 "
                        "확인하세요(doctor 명령).", fast_failures,
                    )
                if not self._sleep(wait, stop_check):
                    return
                continue

            fast_failures = 0
            if self.matched():
                # 최대 녹화 시간에 걸려 끝났지만 수업(대상)은 이어지고 있음 — 바로 이어서 녹화
                log.info("대상이 아직 실행 중이라 이어서 다음 녹화를 시작합니다.")

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
