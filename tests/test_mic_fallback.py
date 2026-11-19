"""동적 마이크 롤백 및 실패 격리 단위 테스트.

실제 오디오 장치나 Windows 의존성 없이 가상 마이크(MockMic)로 모든 분기를 검증합니다.
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


def test_fallback_switches_to_next_available_mic(tmp_path, monkeypatch):
    """마이크 오류 시 실패한 장치를 제외하고 살아있는 대체 마이크로 전환되어야 합니다."""
    cfg = FakeConfig({
        "recording.audio.mic_enabled": True,
        "recording.audio.mic_device": "",
        "recording.audio.mic_auto_select": True,
    })
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_source = MockMic("기본마이크-고장")

    # devices.select_working_mic 대체 모의
    def mock_select(exclude=(), **kw):
        assert "기본마이크-고장" in exclude, "실패한 장치가 제외 목록에 전달되지 않았습니다"
        return MockMic("보조-USB마이크", dead=False), []

    monkeypatch.setattr("gymhall.recorder.devices.select_working_mic", mock_select)

    # 롤백 트리거
    new_mic = session._invalidate_and_fallback_mic(failed_device_name="기본마이크-고장")

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

    # 롤백 트리거
    new_mic = session._invalidate_and_fallback_mic(failed_device_name="전용무선마이크")

    # 장치를 임의로 바꾸지 않고 None으로 안전 격리
    assert new_mic is None
    assert session._mic_source is None
    assert session._mic_fallback_count == 1


def test_fallback_stops_after_max_attempts(tmp_path, monkeypatch):
    """불안정한 드라이버로 인해 롤백이 반복될 경우 상한(2회) 도달 시 추가 프로브 없이 포기해야 합니다."""
    cfg = FakeConfig({
        "recording.audio.mic_enabled": True,
        "recording.audio.mic_device": "",
        "recording.audio.mic_auto_select": True,
    })
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_fallback_count = 2  # 이미 2회 롤백 소진 상태

    probed = False

    def mock_select(**kw):
        nonlocal probed
        probed = True
        return MockMic("또다른마이크"), []

    monkeypatch.setattr("gymhall.recorder.devices.select_working_mic", mock_select)

    new_mic = session._invalidate_and_fallback_mic(failed_device_name="고장장치")

    assert new_mic is None
    assert session._mic_source is None
    assert not probed, "상한 도달 후에도 무한 프로브를 시도했습니다"


def test_handle_audio_track_errors_triggers_fallback(tmp_path, monkeypatch):
    """세그먼트 결과에 마이크 트랙 에러가 있으면 _invalidate_and_fallback_mic이 호출되어야 합니다."""
    cfg = FakeConfig({"recording.audio.mic_enabled": True})
    session = RecordingSession(cfg, Layout(tmp_path), "테스트")
    session._mic_source = MockMic("외장마이크")

    fallback_called = False

    def mock_fallback(failed_device_name="", **kw):
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

    assert fallback_called, "마이크 트랙 에러가 발생했으나 롤백이 트리거되지 않았습니다"
    assert any("마이크 트랙 실패" in w for w in res.warnings)
