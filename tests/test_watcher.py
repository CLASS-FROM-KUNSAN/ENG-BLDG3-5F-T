"""프로그램 감지 트리거.

실제 프로세스 없이 matched() 를 바꿔치기해 시나리오를 만듭니다.
"""

from __future__ import annotations

import threading
import time

from syncnote.recorder.watcher import Watcher


def make(alive_fn, **kw):
    w = Watcher(processes=["zoom.exe"], interval_sec=0.05, stop_grace_sec=0.1, **kw)
    w.matched = lambda: "zoom.exe" if alive_fn() else None  # type: ignore[assignment]
    return w


def test_gives_up_instead_of_thrashing():
    """회귀 방지: 녹화가 즉시 실패하는데 대상이 계속 켜져 있으면
    무한히 재시작하며 빈 폴더를 초당 수백 개 만들어 디스크를 채웁니다.
    """
    w = make(lambda: True)
    calls = {"n": 0}
    started = time.monotonic()

    w.watch(lambda hit: calls.__setitem__("n", calls["n"] + 1),
            cooldown_sec=0.2, min_session_sec=1.0, max_fast_failures=3)

    assert calls["n"] == 3, f"{calls['n']}회 시작됨 — 포기 로직이 동작하지 않았습니다"
    assert time.monotonic() - started > 0.3, "쿨다운 없이 즉시 재시도했습니다"


def test_one_appearance_one_recording():
    """Zoom 을 한 번 켜면 한 번만 녹화해야 합니다."""
    alive = {"v": True}
    w = make(lambda: alive["v"])
    calls = {"n": 0}

    def on_start(hit):
        calls["n"] += 1
        time.sleep(1.2)       # min_session_sec 를 넘겨 '정상 세션' 으로 인정
        alive["v"] = False    # 수업 종료

    stop = {"go": False}
    threading.Timer(4.0, lambda: stop.__setitem__("go", True)).start()
    w.watch(on_start, stop_check=lambda: stop["go"],
            cooldown_sec=0.2, min_session_sec=1.0)

    assert calls["n"] == 1


def test_second_appearance_records_again():
    alive = {"v": True}
    w = make(lambda: alive["v"])
    calls = {"n": 0}

    def on_start(hit):
        calls["n"] += 1
        time.sleep(1.1)
        alive["v"] = False
        if calls["n"] == 1:
            threading.Timer(0.8, lambda: alive.__setitem__("v", True)).start()

    stop = {"go": False}
    threading.Timer(7.0, lambda: stop.__setitem__("go", True)).start()
    w.watch(on_start, stop_check=lambda: stop["go"],
            cooldown_sec=0.1, min_session_sec=1.0)

    assert calls["n"] == 2


def test_wait_until_gone_has_a_ceiling():
    """감지 조건이 너무 넓으면 영원히 멈춥니다. 상한이 필요합니다."""
    w = make(lambda: True)
    started = time.monotonic()
    w.wait_until_gone(max_wait_sec=0.5)
    assert time.monotonic() - started < 3


def test_wait_until_gone_respects_stop():
    w = make(lambda: True)
    stop = {"go": False}
    threading.Timer(0.3, lambda: stop.__setitem__("go", True)).start()
    started = time.monotonic()
    w.wait_until_gone(stop_check=lambda: stop["go"])
    assert time.monotonic() - started < 3


def test_empty_target_list_returns_immediately():
    """감시 대상이 없으면 조용히 무한 루프를 돌지 말고 바로 끝내야 합니다."""
    w = Watcher(processes=[], window_titles=[], interval_sec=0.05)
    calls = {"n": 0}
    w.watch(lambda hit: calls.__setitem__("n", calls["n"] + 1))
    assert calls["n"] == 0


def test_stop_check_honoured_before_first_match():
    w = make(lambda: False)
    w.watch(lambda hit: None, stop_check=lambda: True)


def test_exception_in_on_start_does_not_kill_watcher():
    """녹화가 예외로 죽어도 감시는 계속되거나 깔끔히 포기해야 합니다."""
    w = make(lambda: True)
    calls = {"n": 0}

    def boom(hit):
        calls["n"] += 1
        raise RuntimeError("ffmpeg 폭발")

    w.watch(boom, cooldown_sec=0.05, min_session_sec=1.0, max_fast_failures=2)
    assert calls["n"] == 2


def test_keyboard_interrupt_propagates():
    """Ctrl+C 는 감시를 즉시 끝내야 합니다."""
    import pytest

    w = make(lambda: True)

    def interrupt(hit):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        w.watch(interrupt)


def test_window_title_matching():
    w = Watcher(processes=[], window_titles=["회의 중"], interval_sec=0.05)
    import syncnote.recorder.watcher as mod

    original = mod._window_titles
    try:
        mod._window_titles = lambda: ["Zoom 회의 중 - 자료구조"]
        assert w.matched() == "Zoom 회의 중 - 자료구조"
        mod._window_titles = lambda: ["메모장"]
        assert w.matched() is None
    finally:
        mod._window_titles = original
