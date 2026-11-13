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

import os
import time

import pytest

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


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows 에서는 실제 화면 목록이 나오는 게 정상입니다")
def test_enumerate_is_empty_off_windows():
    """리눅스 CI 에서 ctypes 를 부르면 안 됩니다."""
    assert M.enumerate_monitors() == []


# --- 주 모니터를 1번으로 (V8) -----------------------------------------
# 이 정렬은 _win_monitors 안에 있을 때 리눅스 CI 에서 한 줄도 안
# 돌았습니다. 순수 함수로 빼서 어느 OS 에서나 시험합니다.
def test_primary_comes_first():
    """사람은 주 모니터를 1번이라고 셉니다."""
    got = M.primary_first([mon(9, x=1920), mon(9, x=0, primary=True)])
    assert [m.index for m in got] == [1, 2]
    assert got[0].primary and got[0].x == 0
    assert got[1].x == 1920


def test_indexes_are_renumbered_from_one():
    """들어온 index 를 믿으면 안 됩니다. 다시 매깁니다."""
    got = M.primary_first([mon(7, x=0, primary=True), mon(3, x=100), mon(5, x=200)])
    assert [m.index for m in got] == [1, 2, 3]


def test_already_sorted_stays_put():
    got = M.primary_first([mon(1, x=0, primary=True), mon(2, x=1920)])
    assert [(m.index, m.x) for m in got] == [(1, 0), (2, 1920)]


def test_no_primary_falls_back_to_position():
    """어느 것도 주 모니터가 아니면 왼쪽부터입니다."""
    got = M.primary_first([mon(1, x=1920), mon(2, x=-1920), mon(3, x=0)])
    assert [m.x for m in got] == [-1920, 0, 1920]


def test_an_empty_list_stays_empty():
    assert M.primary_first([]) == []


def test_a_monitor_left_of_primary_is_still_number_two():
    """왼쪽에 있어도 주 모니터가 1번입니다. 좌표순이 아닙니다."""
    got = M.primary_first([mon(1, x=-1920), mon(2, x=0, primary=True)])
    assert got[0].primary and got[0].x == 0
    assert got[1].x == -1920


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


# --- OS 분기 그 자체 ---------------------------------------------------
# win_input_args 는 리눅스 CI 에서 잘 돕니다. 그런데 _input_args 가
# Windows 에서 enumerate_monitors() 를 불러 그걸 넘기는지는 여전히
# 실행되지 않습니다. 한 줄이지만 그 한 줄이 틀리면(인자를 빠뜨리면)
# 테스트는 초록이고 사용자만 압니다 — 이 코드베이스에서 세 번째로
# 같은 모양입니다.
#
# 프로덕션 함수에 _platform / _monitors 같은 테스트 전용 인자를 넣는
# 대신 os.name 을 바꿔 끼웁니다. _input_args 가 그걸 호출 시점에
# 읽으므로 **진짜 함수가 진짜 분기를 탑니다.** 서명이 깨끗하게
# 남는다는 점에서 주입보다 낫습니다.
def test_input_args_dispatches_to_the_windows_builder(monkeypatch):
    import os as os_mod

    from gymhall.recorder import video as V

    monkeypatch.setattr(os_mod, "name", "nt")
    monkeypatch.setattr(V, "enumerate_monitors",
                        lambda: [mon(1, primary=True), mon(2, x=1920, w=1280, h=720)])
    args = VideoRecorder(out_dir="/tmp", backend="gdigrab", monitor=2)._input_args()
    assert args[args.index("-offset_x") + 1] == "1920"
    assert args[args.index("-video_size") + 1] == "1280x720"


def test_input_args_passes_the_monitor_list_through(monkeypatch):
    """목록을 안 넘기면 monitor 설정이 통째로 죽습니다."""
    import os as os_mod

    from gymhall.recorder import video as V

    monkeypatch.setattr(os_mod, "name", "nt")
    monkeypatch.setattr(V, "enumerate_monitors", lambda: [])
    args = VideoRecorder(out_dir="/tmp", backend="gdigrab", monitor=2)._input_args()
    assert "-offset_x" not in args and args[-1] == "desktop"


def test_input_args_uses_x11_off_windows(monkeypatch):
    import os as os_mod

    monkeypatch.setattr(os_mod, "name", "posix")
    args = VideoRecorder(out_dir="/tmp", backend="gdigrab", monitor=2)._input_args()
    assert "x11grab" in args


def test_a_broken_monitor_api_does_not_stop_recording(monkeypatch):
    """모니터 선택 실패가 녹화 실패가 되면 안 됩니다.

    전체 화면이 찍히는 것이 아무것도 안 찍히는 것보다 낫습니다.
    원격 데스크톱이나 DWM 이 꺼진 환경에서 WinAPI 가 터질 수 있습니다.
    """
    import os as os_mod

    from gymhall.recorder import monitors as MM

    monkeypatch.setattr(os_mod, "name", "nt")
    monkeypatch.setattr(MM, "_win_monitors",
                        lambda: (_ for _ in ()).throw(OSError("DWM 없음")))
    assert MM.enumerate_monitors() == []
    args = VideoRecorder(out_dir="/tmp", backend="gdigrab", monitor=2)._input_args()
    assert args[-1] == "desktop", "예외 때문에 녹화가 통째로 죽었습니다"


# conftest 가 gymhall 로거를 WARNING 으로 묶어 둡니다. logger 를 안
# 적으면 caplog 가 root 만 낮춰서 INFO 가 레벨 검사에서 먼저 죽고,
# 테스트는 "로그가 없다" 가 아니라 "못 봤다" 로 빨개집니다.
def test_ddagrab_says_monitor_zero_means_primary(caplog):
    """gdigrab 의 desktop 과 뜻이 다릅니다. 모르면 보조 모니터를 찾아 헤맵니다."""
    with caplog.at_level("INFO", logger="gymhall"):
        cmd([mon(1), mon(2, x=1920)], backend="ddagrab", monitor=0)
    assert "주 모니터 하나만" in caplog.text, caplog.text


def test_gdigrab_desktop_says_nothing_of_the_sort(caplog):
    """gdigrab 의 desktop 은 전체 화면이 맞으므로 안내가 틀립니다."""
    with caplog.at_level("INFO", logger="gymhall"):
        cmd([mon(1)], backend="gdigrab", monitor=0)
    assert "주 모니터 하나만" not in caplog.text


def test_ddagrab_with_a_monitor_number_says_nothing(caplog):
    with caplog.at_level("INFO", logger="gymhall"):
        args = cmd([mon(1), mon(2, x=1920)], backend="ddagrab", monitor=2)
    assert "output_idx=1" in args[-1]
    assert "주 모니터 하나만" not in caplog.text


# --- 수동 녹화에는 pre-roll 이 붙으면 안 됩니다 -----------------------
def test_manual_record_start_gets_no_pre_roll():
    """사용자가 '지금부터' 를 누른 것이지 '2분 뒤부터' 가 아닙니다.

    schedule 만 pre-roll 을 씁니다. 수동 녹화에 붙으면 누르고 나서
    2분 동안 화면이 안 찍히는데 사용자는 이유를 모릅니다.
    """
    import inspect

    from gymhall import cli

    src = inspect.getsource(cli.cmd_record_start)
    assert "video_not_before" not in src, "수동 녹화에 pre-roll 이 붙었습니다"
    assert "pre_roll" not in src

    # 기본값이 0 이어야 _run_recording 을 그냥 불러도 안 기다립니다.
    sig = inspect.signature(cli._run_recording)
    assert sig.parameters["video_not_before"].default == 0.0


def test_watch_mode_gets_no_pre_roll_either():
    """watch 는 '프로그램이 떴다' 가 시작 신호입니다. 당길 앞이 없습니다."""
    import inspect

    from gymhall import cli

    assert "video_not_before" not in inspect.getsource(cli.cmd_record_watch)


# --- ddagrab 화면 번호는 믿을 수 없습니다 -----------------------------
# primary_first() 는 "주 모니터 먼저" 로 번호를 다시 매기고, ddagrab 의
# output_idx 는 그래픽 드라이버가 정한 D3D11 출력 순서입니다. 둘이
# 같다는 근거가 없습니다. 모니터가 하나면 둘 다 0 이라 차이가 안 나서
# 1대 PC 에서는 원리적으로 검증할 수 없습니다.
#
# 그래서 "맞는지" 가 아니라 "사용자에게 알리는지" 를 검사합니다.
def test_ddagrab_warns_when_a_monitor_number_may_not_match(caplog):
    with caplog.at_level("WARNING", logger="gymhall"):
        cmd([mon(1, primary=True), mon(2, x=1920)], backend="ddagrab", monitor=2)
    assert "드라이버가 정한 순서" in caplog.text, caplog.text
    assert "gdigrab" in caplog.text, "대안을 안 알려 줍니다"
    # 쓴 값을 적어야 2대 PC 에서 역으로 짝을 알아낼 수 있습니다.
    assert "monitor=2" in caplog.text and "output_idx=1" in caplog.text, caplog.text


def test_a_single_monitor_needs_no_such_warning(caplog):
    """화면이 하나면 번호가 0 하나뿐이라 틀릴 수가 없습니다."""
    with caplog.at_level("WARNING", logger="gymhall"):
        cmd([mon(1, primary=True)], backend="ddagrab", monitor=1)
    assert "드라이버가 정한 순서" not in caplog.text


def test_gdigrab_never_warns_about_driver_order(caplog):
    """gdigrab 은 좌표로 잘라내므로 이 문제가 없습니다."""
    with caplog.at_level("WARNING", logger="gymhall"):
        cmd([mon(1, primary=True), mon(2, x=1920)], backend="gdigrab", monitor=2)
    assert "드라이버가 정한 순서" not in caplog.text
