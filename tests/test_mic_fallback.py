"""동적 마이크 전환(폴백) 단위 테스트.

실제 오디오 장치나 Windows 의존성 없이 가상 마이크(MockMic)로 모든 분기를 검증합니다.
실패 장치를 인위적으로 배제하지 않아도, 신호가 0이거나 고장 난 장치는
select_working_mic 의 프로브 단계에서 자동으로 걸러져 다음 장치로 전환됩니다.
"""
from __future__ import annotations

from pathlib import Path

from gymhall.paths import Layout
from gymhall.recorder.audio import TrackResult
from gymhall.recorder.session import RecordingSession, SessionResult


class FakeConfig:
    def __init__(self, data: dict | None = None):
        self._d = data or {}

    def get(self, key: str, default=None):
        return self._d.get(key, default)


class MockMic:
    def __init__(self, name: str, dead: bool = False):
        self.name = name
        self.dead = dead


def test_fallback_switches_to_next_live_mic(tmp_path, monkeypatch):
    """마이크 오류 시 캐시를 무효화하고 살아있는 대체 마이크로 전환되어야 합니다."""
    cfg = FakeConfig({
        "recording.audio.mic_enabled": True,
        "recording.audio.mic_device": "",
        "recording.audio.mic_auto_select": True,
    })
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_source = MockMic("기본마이크-고장")

    # devices.select_working_mic 대체 모의: 고장난 장치는 건너뛰고 살아있는 장치 반환
    def mock_select(**kw):
        return MockMic("보조-USB마이크", dead=False), []

    monkeypatch.setattr("gymhall.recorder.devices.select_working_mic", mock_select)

    # 롤백/전환 트리거
    new_mic = session._invalidate_and_fallback_mic()

    assert new_mic is not None
    assert new_mic.name == "보조-USB마이크"
    assert session._mic_source == new_mic
    assert session._mic_fallback_count == 1


def test_explicit_mic_device_is_never_swapped_silently(tmp_path):
    """사용자가 mic_device를 명시했거나 auto_select=false인 경우 임의로 장치를 바꾸지 않고 비활성화해야 합니다."""
    cfg = FakeConfig({
        "recording.audio.mic_enabled": True,
        "recording.audio.mic_device": "전용무선마이크",
        "recording.audio.mic_auto_select": True,
    })
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_source = MockMic("전용무선마이크")

    # 전환 트리거
    new_mic = session._invalidate_and_fallback_mic()

    # 장치를 임의로 바꾸지 않고 None으로 안전 격리
    assert new_mic is None
    assert session._mic_source is None
    assert session._mic_fallback_count == 1


def test_dead_device_automatically_skipped_without_exclusion(tmp_path, monkeypatch):
    """신호가 0인 장치는 굳이 배제 목록을 쓰지 않아도 select_working_mic 에서 자동으로 다음 장치로 넘어갑니다."""
    from gymhall.recorder import devices

    class FakeProbeRec:
        def __init__(self, level):
            self.level = level

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def record(self, numframes):
            import numpy as np

            if self.level == 0.0:
                return np.zeros((numframes, 2), dtype=np.float32)
            return np.ones((numframes, 2), dtype=np.float32) * self.level

    class ProbedMic:
        def __init__(self, name, level):
            self.name = name
            self.level = level

        def recorder(self, samplerate, channels, blocksize):
            return FakeProbeRec(self.level)

    dead_default = ProbedMic("기본마이크-무음", level=0.0)
    live_usb = ProbedMic("USB외장마이크", level=0.5)

    # 실제 select_working_mic 호출: 배제 목록 없이도 dead_default 는 넘어가고 live_usb 가 잡혀야 함
    chosen, probes = devices.select_working_mic(
        duration_sec=0.01,
        default=dead_default,
        mics=[live_usb],
    )

    assert chosen.name == "USB외장마이크"
    assert probes[0].dead is True
    assert probes[1].dead is False


def test_handle_audio_track_errors_triggers_fallback(tmp_path, monkeypatch):
    """세그먼트 결과에 마이크 트랙 에러가 있으면 _invalidate_and_fallback_mic이 호출되어야 합니다."""
    cfg = FakeConfig({"recording.audio.mic_enabled": True})
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_source = MockMic("외장마이크")

    fallback_called = False

    def mock_fallback(**kw):
        nonlocal fallback_called
        fallback_called = True
        return

    monkeypatch.setattr(session, "_invalidate_and_fallback_mic", mock_fallback)

    res = SessionResult(label="테스트", started_at=__import__("datetime").datetime.now())
    tracks = [
        TrackResult(name="system", path=Path("/tmp/sys.wav"), error=None),
        TrackResult(name="mic", path=Path("/tmp/mic.wav"), error="장치가 분리되었습니다"),
    ]

    session._handle_audio_track_errors(tracks, res)

    assert fallback_called, "마이크 트랙 에러가 발생했으나 전환이 트리거되지 않았습니다"
    assert any("마이크 트랙 실패" in w for w in res.warnings)
