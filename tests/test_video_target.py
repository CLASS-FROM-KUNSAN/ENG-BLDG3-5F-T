"""화면 캡처 대상과 시작 시각.

두 가지를 봅니다.

1. **어느 화면을 잡는가.** 강의실 PC 는 대개 두 화면입니다. `-i desktop`
   은 둘을 합친 가상 바탕화면을 잡아서, 빔프로젝터 쪽이 절반으로 줄고
   교탁 화면의 메일까지 같이 올라갑니다.
2. **언제 시작하는가.** pre_roll_minutes=2 는 지금까지 화면까지 2분
   당겨 켰습니다. 그 2분 동안 강의실 PC 앞에 앉은 사람의 화면이
   그대로 찍혀 릴리스로 갔습니다.
"""

from __future__ import annotations

import time

from gymhall.recorder import monitors as M
from gymhall.recorder.video import VideoRecorder


def mon(i, x=0, y=0, w=1920, h=1080, primary=False):
    return M.Monitor(i, x, y, w, h, primary)


def cmd(monitors=(), **kw):
    """Windows 입력 인자를 어느 OS 에서나 만들어 봅니다.

    _input_args() 를 부르면 리눅스에서는 x11grab 으로 빠져서 이 코드가
    한 줄도 안 돕니다. 그래서 화면 목록을 인자로 받는 쪽을 부릅니다.
    """
    return VideoRecorder(out_dir="/tmp", **kw).win_input_args(list(monitors))


# --- 화면 고르기 ------------------------------------------------------
def test_zero_means_the_whole_desktop():
    """지금까지의 동작입니다. 바뀌면 안 됩니다."""
    args = cmd([mon(1), mon(2, x=1920)], backend="gdigrab", monitor=0)
    assert "-offset_x" not in args
    assert args[-1] == "desktop"


def test_a_monitor_is_cropped_out_of_the_virtual_desktop():
    args = cmd([mon(1, primary=True), mon(2, x=1920, w=1280, h=720)],
               backend="gdigrab", monitor=2)
    assert args[args.index("-offset_x") + 1] == "1920"
    assert args[args.index("-offset_y") + 1] == "0"
    assert args[args.index("-video_size") + 1] == "1280x720"
    assert args[-1] == "desktop"


def test_a_monitor_left_of_the_primary_has_a_negative_offset():
    """보조 모니터가 왼쪽이면 x 가 음수입니다. 깎으면 엉뚱한 데를 찍습니다."""
    args = cmd([mon(1, primary=True), mon(2, x=-1920)],
               backend="gdigrab", monitor=2)
    assert args[args.index("-offset_x") + 1] == "-1920"


def test_an_out_of_range_monitor_falls_back_loudly(caplog):
    """조용히 1번으로 떨어지면 한 학기 내내 엉뚱한 화면을 찍습니다."""
    with caplog.at_level("WARNING"):
        args = cmd([mon(1, primary=True)], backend="gdigrab", monitor=5)
    assert "-offset_x" not in args, "범위 밖인데 아무 화면이나 잡았습니다"
    assert "monitor=5" in caplog.text, caplog.text


def test_unknown_monitor_list_keeps_the_old_behaviour(caplog):
    """목록을 못 읽는 것은 '화면이 없다' 가 아니라 '모르겠다' 입니다.

    그래서 경고를 찍으면 안 됩니다. 설정이 틀린 게 아니라 우리가 못
    읽은 것이고, 리눅스 개발 환경에서는 매 실행마다 뜹니다.
    """
    with caplog.at_level("WARNING"):
        args = cmd([], backend="gdigrab", monitor=2)
    assert "-offset_x" not in args and args[-1] == "desktop"
    assert "monitor=2" not in caplog.text, caplog.text


def test_window_title_beats_monitor():
    """창 하나가 화면 하나보다 좁습니다. 좁은 쪽이 이겨야 합니다."""
    args = cmd([mon(1), mon(2, x=1920)], backend="gdigrab", monitor=2,
               window_title="강의자료.pptx")
    assert "-offset_x" not in args
    assert any(a.startswith("title=") for a in args), args


def test_ddagrab_selects_by_output_index():
    """ddagrab 은 잘라내기가 아니라 output_idx 로 고릅니다(0부터)."""
    args = cmd([mon(1), mon(2, x=1920)], backend="ddagrab", monitor=2)
    assert "output_idx=1" in args[-1], args


def test_pick_is_one_based():
    mons = [mon(1), mon(2)]
    assert M.pick(mons, 1).index == 1
    assert M.pick(mons, 2).index == 2
    assert M.pick(mons, 0) is None
    assert M.pick(mons, 3) is None
    assert M.pick([], 1) is None


def test_enumerate_is_empty_off_windows():
    """리눅스 CI 에서 ctypes 를 부르면 안 됩니다."""
    assert M.enumerate_monitors() == []


# --- 화면 시작 시각 ---------------------------------------------------
class Cfg:
    def __init__(self, d=None):
        self._d = d or {}

    def get(self, k, default=None):
        return self._d.get(k, default)


def session(tmp_path, **kw):
    from gymhall.paths import Layout
    from gymhall.recorder.session import RecordingSession

    return RecordingSession(Cfg(), Layout(tmp_path), "수업", **kw)


def test_video_waits_until_the_real_class_time(tmp_path):
    s = session(tmp_path, video_not_before=time.time() + 0.4)
    t0 = time.time()
    s._wait_for_video_start()
    assert time.time() - t0 >= 0.3, "기다리지 않았습니다"


def test_waiting_happens_only_once(tmp_path):
    """두 번째 세그먼트에서 또 기다리면 30분짜리 구멍이 납니다."""
    s = session(tmp_path, video_not_before=time.time() + 0.3)
    s._wait_for_video_start()
    t0 = time.time()
    s._wait_for_video_start()
    assert time.time() - t0 < 0.1, "두 번째에도 기다렸습니다"


def test_the_wait_is_disarmed_even_when_cut_short(tmp_path):
    """시각이 지나서 안 기다리는 것과, 해제된 것은 다릅니다.

    정지 신호로 일찍 빠져나오면 시각은 아직 미래입니다. 해제하지
    않으면 다음 세그먼트가 그 시각까지 또 기다립니다.
    """
    s = session(tmp_path, video_not_before=time.time() + 30)
    s.request_stop()
    s._wait_for_video_start()
    assert s.video_not_before == 0.0, "다음 세그먼트에서 또 기다립니다"


def test_no_pre_roll_means_no_wait(tmp_path):
    s = session(tmp_path)
    t0 = time.time()
    s._wait_for_video_start()
    assert time.time() - t0 < 0.1


def test_a_past_time_does_not_wait(tmp_path):
    s = session(tmp_path, video_not_before=time.time() - 100)
    t0 = time.time()
    s._wait_for_video_start()
    assert time.time() - t0 < 0.1


def test_stopping_cuts_the_wait_short(tmp_path):
    """Ctrl+C 를 눌렀는데 2분을 더 기다리면 안 됩니다."""
    s = session(tmp_path, video_not_before=time.time() + 30)
    s.request_stop()
    t0 = time.time()
    s._wait_for_video_start()
    assert time.time() - t0 < 1.0, "정지 신호를 무시했습니다"


def test_audio_is_not_delayed(tmp_path):
    """소리는 당겨서 받아야 수업이 일찍 시작해도 안 놓칩니다.

    화면만 늦추는 것이 핵심입니다. 둘 다 늦추면 pre-roll 이 통째로
    없어지고, 둘 다 당기면 프라이버시 문제가 그대로입니다.
    """
    import inspect

    from gymhall.recorder.session import RecordingSession

    src = inspect.getsource(RecordingSession._record_segment)
    assert src.index("audio_rec.start(") < src.index("video_rec.start("), \
        "화면을 소리보다 먼저 켜고 있습니다 — pre-roll 동안 화면이 찍힙니다"
