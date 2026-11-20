"""다중 모니터 독립 녹화 — session._record_segment 의 다중 VideoRecorder 경로.

실제 ffmpeg 를 띄우지 않습니다. VideoRecorder.start / stop 을 모두 패치해서
단위 테스트만 합니다.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncnote.paths import Layout
from syncnote.recorder.session import RecordingSession


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
class Cfg:
    """최소 설정 객체."""

    def __init__(self, d: dict | None = None):
        self._d = d or {}

    def get(self, key: str, default=None):
        return self._d.get(key, default)

    def section(self, key: str) -> dict:
        v = self.get(key, {})
        return v if isinstance(v, dict) else {}


def _session(tmp_path: Path, cfg_data: dict | None = None) -> RecordingSession:
    cfg = Cfg(cfg_data or {})
    return RecordingSession(cfg, Layout(tmp_path), "테스트수업")


def _base_cfg(monitors: list[int] | None = None, monitor: int = 0) -> dict:
    """세그먼트 하나를 돌릴 최소 설정."""
    return {
        "recording.video.monitors": monitors if monitors is not None else [],
        "recording.video.monitor": monitor,
        "recording.video.backend": "gdigrab",
        "recording.video.fps": 10,
        "recording.video.scale_width": 0,
        "recording.video.scale_height": 0,
        "recording.video.preset": "veryfast",
        "recording.video.crf": 30,
        "recording.video.window_title": "",
        "recording.video.draw_mouse": True,
        "recording.audio.samplerate": 48000,
        "recording.audio.channels": 2,
        "recording.audio.system_enabled": True,
        "recording.audio.system_device": "",
        "recording.audio.system_gain_db": 0.0,
        "recording.audio.mic_enabled": False,
        "recording.audio.mic_device": "",
        "recording.audio.mic_gain_db": 0.0,
        "recording.audio.silence_warn_sec": 60,
        "recording.audio.silence_peak": 1e-5,
        "recording.audio.track_layout": "separate",
        "transcribe.source_track": "system",
        "transcribe.enabled": False,
    }


def _make_fake_vr_class(created: list[int]):
    """VideoRecorder 를 흉내 내는 클래스. created 에 monitor 번호를 기록합니다."""
    class FakeVR:
        def __init__(self, *, monitor, **_kw):
            created.append(monitor)
            self._mon = monitor
            self._started_at = time.time()

        def start(self, stem):
            return Path(f"/fake/{stem}_mon{self._mon}.mkv")

        def stop(self, timeout=20.0):
            return None

        @property
        def is_running(self):
            return False

        @property
        def started_at(self):
            return self._started_at

    return FakeVR


def _run_segment(tmp_path, cfg_data, *, mux_side_effect=None):
    """_record_segment 를 패치된 환경에서 실행하고 (mux 호출 경로 목록, created 목록)을 반환."""
    from syncnote.recorder import ffmpeg_util as fu
    from syncnote.recorder import session as sess_mod

    mux_calls: list[Path] = []
    created: list[int] = []
    FakeVR = _make_fake_vr_class(created)

    def _default_mux(out_path, *a, **kw):
        p = Path(out_path)
        p.touch()
        mux_calls.append(p)

    s = _session(tmp_path, cfg_data)

    with patch.object(sess_mod, "VideoRecorder", FakeVR), \
         patch.object(sess_mod, "AudioRecorder", MagicMock()) as MockAR, \
         patch.object(sess_mod, "mux", side_effect=mux_side_effect or _default_mux), \
         patch.object(fu, "duration_of", return_value=10.0):

        ar_inst = MagicMock()
        ar_inst.stop.return_value = []
        ar_inst.started_at = time.time()
        MockAR.return_value = ar_inst

        import datetime as dt_mod

        from syncnote.recorder.session import SessionResult
        result = SessionResult(label="테스트수업", started_at=dt_mod.datetime.now())
        s._record_segment(1, tmp_path, 0.001, "mkv", True, True, result)

    return mux_calls, created


# ---------------------------------------------------------------------------
# monitors 목록 결정 — 단위 테스트
# ---------------------------------------------------------------------------
class TestMonitorListResolution:
    """_record_segment 가 monitors / monitor 를 올바르게 고르는지."""

    def test_single_monitor_zero_uses_full_desktop(self, tmp_path):
        """monitor=0 (전체 화면)은 VideoRecorder(monitor=0) 하나."""
        _, created = _run_segment(tmp_path, _base_cfg(monitors=[], monitor=0))
        assert created == [0]

    def test_single_monitor_nonzero(self, tmp_path):
        """monitors 비어 있고 monitor=2 -> VideoRecorder(monitor=2) 하나."""
        _, created = _run_segment(tmp_path, _base_cfg(monitors=[], monitor=2))
        assert created == [2]

    def test_two_monitors_creates_two_recorders(self, tmp_path):
        """monitors=[1,2] -> VideoRecorder 두 개(monitor=1, monitor=2)."""
        _, created = _run_segment(tmp_path, _base_cfg(monitors=[1, 2], monitor=0))
        assert sorted(created) == [1, 2]

    def test_three_monitors(self, tmp_path):
        """monitors=[1,2,3] -> VideoRecorder 세 개."""
        _, created = _run_segment(tmp_path, _base_cfg(monitors=[1, 2, 3], monitor=0))
        assert sorted(created) == [1, 2, 3]


# ---------------------------------------------------------------------------
# 출력 파일명 suffix — 단위 테스트
# ---------------------------------------------------------------------------
class TestOutputFilenames:
    """다중 모니터 시 _mon<N> suffix 가 붙는지."""

    def test_single_monitor_no_suffix(self, tmp_path):
        """모니터 하나: suffix 없음."""
        calls, _ = _run_segment(tmp_path, _base_cfg(monitors=[], monitor=0))
        assert len(calls) == 1
        assert "_mon" not in calls[0].name

    def test_two_monitors_have_mon_suffixes(self, tmp_path):
        """monitors=[1,2]: _mon1, _mon2 suffix."""
        calls, _ = _run_segment(tmp_path, _base_cfg(monitors=[1, 2]))
        names = [c.name for c in calls]
        assert len(calls) == 2
        assert any("_mon1" in n for n in names), names
        assert any("_mon2" in n for n in names), names

    def test_two_monitors_produce_independent_files(self, tmp_path):
        """두 파일의 경로가 서로 달라야 합니다."""
        calls, _ = _run_segment(tmp_path, _base_cfg(monitors=[1, 2]))
        assert calls[0] != calls[1]

    def test_mux_called_once_per_monitor(self, tmp_path):
        """mux 는 모니터 수만큼 불려야 합니다."""
        calls, _ = _run_segment(tmp_path, _base_cfg(monitors=[1, 2, 3]))
        assert len(calls) == 3


# ---------------------------------------------------------------------------
# 설정 검증 — monitors 목록
# ---------------------------------------------------------------------------
class TestConfigValidation:
    """config.validate 가 monitors 목록의 잘못된 값을 거르는지."""

    def _load_with(self, monitors_val):
        """DEFAULTS 에 monitors 를 덮어 씌워 validate 까지 돌립니다."""
        import copy

        from syncnote import config as cfg_mod

        data = copy.deepcopy(cfg_mod.DEFAULTS)
        data["recording"]["video"]["monitors"] = monitors_val
        # validate 를 위해 필수 필드 채우기
        data["github"]["owner"] = "x"
        data["github"]["repo"] = "y"
        data["eclass"]["login_url"] = "https://example.com"
        data["eclass"]["username"] = "u"
        cfg = cfg_mod.Config(data=data)
        cfg_mod.validate(cfg)   # 오류 없으면 통과

    def test_empty_list_is_valid(self):
        self._load_with([])

    def test_valid_two_monitor_list(self):
        self._load_with([1, 2])

    def test_non_list_is_rejected(self):
        from syncnote.config import ConfigError
        with pytest.raises(ConfigError, match="monitors"):
            self._load_with("1,2")

    def test_zero_in_list_is_rejected(self):
        from syncnote.config import ConfigError
        with pytest.raises(ConfigError, match="monitors"):
            self._load_with([0, 1])

    def test_negative_in_list_is_rejected(self):
        from syncnote.config import ConfigError
        with pytest.raises(ConfigError, match="monitors"):
            self._load_with([-1, 1])

    def test_duplicate_in_list_is_rejected(self):
        from syncnote.config import ConfigError
        with pytest.raises(ConfigError, match="중복"):
            self._load_with([1, 1])


# ---------------------------------------------------------------------------
# 병렬 시작 — 스레드 안전성 (smoke)
# ---------------------------------------------------------------------------
def test_two_recorders_start_in_parallel(tmp_path):
    """두 VideoRecorder 가 실제로 별도 스레드에서 동시에 시작되는지 확인합니다.

    start() 가 호출된 스레드 ID 를 기록해서, 서로 다른 스레드면 병렬입니다.
    """
    start_threads: list[int] = []
    lock = threading.Lock()

    from syncnote.recorder import ffmpeg_util as fu
    from syncnote.recorder import session as sess_mod

    class ThreadTrackingVR:
        def __init__(self, *, monitor, **_kw):
            self._mon = monitor
            self._started_at = time.time()

        def start(self, stem):
            with lock:
                start_threads.append(threading.get_ident())
            time.sleep(0.05)   # 겹치도록 일부러 늦춥니다
            return Path(f"/fake/{stem}_mon{self._mon}.mkv")

        def stop(self, timeout=20.0):
            return None

        @property
        def is_running(self):
            return False

        @property
        def started_at(self):
            return self._started_at

    def _mux(out_path, *a, **kw):
        Path(out_path).touch()

    cfg_data = _base_cfg(monitors=[1, 2])
    s = _session(tmp_path, cfg_data)

    with patch.object(sess_mod, "VideoRecorder", ThreadTrackingVR), \
         patch.object(sess_mod, "AudioRecorder", MagicMock()) as MockAR, \
         patch.object(sess_mod, "mux", side_effect=_mux), \
         patch.object(fu, "duration_of", return_value=10.0):

        ar_inst = MagicMock()
        ar_inst.stop.return_value = []
        ar_inst.started_at = time.time()
        MockAR.return_value = ar_inst

        import datetime as dt_mod

        from syncnote.recorder.session import SessionResult
        result = SessionResult(label="테스트수업", started_at=dt_mod.datetime.now())
        s._record_segment(1, tmp_path, 0.001, "mkv", True, True, result)

    assert len(start_threads) == 2, "VideoRecorder 두 개가 시작되지 않았습니다"
    assert len(set(start_threads)) == 2, "두 start() 가 같은 스레드에서 순차 실행됐습니다"
