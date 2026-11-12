"""모니터 목록 (Windows).

강의실 PC 는 대개 두 화면입니다 — 교탁 모니터와 빔프로젝터. 판서와
슬라이드는 보조 화면에 뜨는데, gdigrab 의 `-i desktop` 은 **두 화면을
합친 가상 바탕화면 전체**를 잡습니다. 그러면 녹화가 가로로 두 배가
되고, 정작 봐야 할 쪽은 절반 크기로 줄어듭니다. 반대로 교탁 화면에
교수의 메일이 떠 있으면 그것까지 같이 올라갑니다.

window_title 을 적어 둔 PC 는 이미 안전합니다(그 창만 잡습니다).
문제는 window_title 이 비어 있는 PC 입니다.

여기서는 EnumDisplayMonitors 로 각 화면의 위치와 크기를 읽습니다.
ffmpeg 에는 그 사각형을 -offset_x/-offset_y/-video_size 로 넘깁니다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..logging_setup import get as get_logger

log = get_logger("recorder.monitors")


@dataclass(frozen=True)
class Monitor:
    index: int
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"

    def describe(self) -> str:
        mark = " (주 모니터)" if self.primary else ""
        return f"{self.index}: {self.size} @ ({self.x},{self.y}){mark}"


def enumerate_monitors() -> list[Monitor]:
    """화면 목록. Windows 가 아니거나 못 읽으면 빈 리스트입니다.

    빈 리스트는 "화면이 없다" 가 아니라 "모르겠다" 입니다. 부르는 쪽은
    그때 전체 바탕화면을 잡는 지금 동작으로 돌아가야 합니다.
    """
    if os.name != "nt":
        return []
    try:
        return _win_monitors()
    except Exception as exc:  # pragma: no cover - Windows 전용
        log.warning("모니터 목록을 읽지 못했습니다: %s", exc)
        return []


def _win_monitors() -> list[Monitor]:  # pragma: no cover - Windows 전용
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # 고DPI 에서 좌표가 축소돼 나오면 잘라낸 사각형이 어긋납니다.
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    MONITORINFOF_PRIMARY = 0x1
    found: list[Monitor] = []

    CALLBACK = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(RECT), ctypes.c_double,
    )

    def _cb(hmon, _hdc, _lprc, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(Monitor(
                index=len(found) + 1,
                x=r.left, y=r.top,
                width=r.right - r.left, height=r.bottom - r.top,
                primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
            ))
        return 1

    user32.EnumDisplayMonitors(None, None, CALLBACK(_cb), 0)
    return primary_first(found)


def primary_first(found: list[Monitor]) -> list[Monitor]:
    """주 모니터를 1번으로 두고 번호를 다시 매깁니다.

    사람은 주 모니터를 "1번" 이라고 셉니다. EnumDisplayMonitors 는
    그 순서를 보장하지 않으므로, 설정에 적은 `monitor = 1` 이 엉뚱한
    화면을 가리킬 수 있습니다.

    이 정렬이 _win_monitors 안에 있을 때는 리눅스 CI 에서 한 줄도 안
    돌았습니다(그 함수 전체가 Windows 전용). 순수 함수로 빼면
    어느 OS 에서나 시험할 수 있습니다 — docs/TESTING.md 원칙 2.
    """
    ordered = sorted(found, key=lambda m: (not m.primary, m.x, m.y))
    return [
        Monitor(i + 1, m.x, m.y, m.width, m.height, m.primary)
        for i, m in enumerate(ordered)
    ]


def pick(monitors: list[Monitor], want: int) -> Monitor | None:
    """want 번째 화면. 0 이면 '전체', 범위를 벗어나면 None.

    범위를 벗어났을 때 조용히 1번으로 떨어지면, 설정이 틀린 것을
    모른 채 한 학기 내내 엉뚱한 화면을 찍습니다.
    """
    if want <= 0:
        return None
    if not monitors:
        # 목록을 못 읽은 것("모르겠다")이지 설정이 틀린 것이 아닙니다.
        # 여기서 경고하면 리눅스 개발 환경에서 매번 뜹니다.
        return None
    for m in monitors:
        if m.index == want:
            return m
    log.warning(
        "monitor=%d 로 설정했는데 화면이 %d개뿐입니다. 전체 바탕화면을 잡습니다. "
        "python -m gymhall devices 로 번호를 확인하세요.",
        want, len(monitors),
    )
    return None
