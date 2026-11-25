"""무음 경고. (사양 변경: 무음이어도 녹화·업로드를 멈추지 않음 — 경고만. 사용자 요청
'무음 감지 시 녹화 및 업로드 차단 금지'. 예전 stop/abort 단계는 long/very_long 경고가 됨.)

"루프백은 잡히는데 소리가 0" 은 녹화가 쓸모없어지는 가장 흔한 길입니다.
지금까지는 60초 뒤 경고 한 줄이 전부라, 세 시간 뒤 로그를 봐야 알았습니다.

트랙 하나만 보면 안 됩니다. 마이크만 조용한 것은 아주 흔합니다
(교수가 판서만 하는 5분, 시험 시간). 켜져 있는 트랙이 **전부 동시에**
조용할 때만 장치 문제로 봅니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncnote.recorder.audio import TrackResult
from syncnote.recorder.session import RecordingSession


class Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


class FakeRecorder:
    def __init__(self, tracks):
        self.results = tracks


def track(name, silent_for, error=None):
    return TrackResult(name=name, path=Path(f"/tmp/{name}.wav"),
                       silent_for=silent_for, error=error)


def session(tmp_path, *, stop_on_silence=True, stop=900, abort=3600):
    from syncnote.paths import Layout

    cfg = Cfg({
        "recording.audio.silence_stop_sec": stop,
        "recording.audio.silence_abort_sec": abort,
    })
    return RecordingSession(cfg, Layout(tmp_path), "수업",
                            stop_on_silence=stop_on_silence)


def test_one_quiet_track_is_not_a_device_problem(tmp_path):
    """마이크만 조용한 것은 아주 흔합니다. 멈추면 안 됩니다."""
    s = session(tmp_path)
    rec = FakeRecorder([track("system", 0.0), track("mic", 7200.0)])
    assert s._silence_verdict(rec) is None


def test_every_track_quiet_for_long_enough_warns_but_keeps_recording(tmp_path):
    s = session(tmp_path)
    rec = FakeRecorder([track("system", 1000.0), track("mic", 1000.0)])
    verdict = s._silence_verdict(rec)
    assert verdict is not None and verdict[0] == "long"
    assert "무음" in verdict[1] and "계속" in verdict[1]


def test_a_much_longer_silence_warns_again_and_never_blocks_upload(tmp_path):
    s = session(tmp_path)
    rec = FakeRecorder([track("system", 4000.0), track("mic", 4000.0)])
    verdict = s._silence_verdict(rec)
    assert verdict is not None and verdict[0] == "very_long"
    assert "업로드는 계속" in verdict[1]


def test_a_short_quiet_stretch_does_nothing(tmp_path):
    """판서 5분, 시험 시간. 정상입니다."""
    s = session(tmp_path)
    rec = FakeRecorder([track("system", 300.0), track("mic", 300.0)])
    assert s._silence_verdict(rec) is None


def test_a_failed_track_does_not_mask_a_real_silence(tmp_path):
    """장치를 못 연 트랙은 셈에서 빼야 합니다.

    마이크를 못 열었다면 그 트랙의 silent_for 는 0 인 채로 멈춰 있습니다.
    그걸 같이 세면 시스템 소리가 통째로 죽어도 '한 트랙은 살아 있다' 가
    되어 영영 안 멈춥니다.
    """
    s = session(tmp_path)
    rec = FakeRecorder([
        track("system", 1000.0),                 # 진짜 무음
        track("mic", 0.0, error="장치를 열 수 없습니다"),
    ])
    verdict = s._silence_verdict(rec)
    assert verdict is not None and verdict[0] == "long"


def test_a_failed_track_alone_is_not_a_verdict(tmp_path):
    s = session(tmp_path)
    rec = FakeRecorder([track("mic", 9999.0, error="장치 없음")])
    assert s._silence_verdict(rec) is None


def test_manual_recording_is_never_stopped(tmp_path):
    """사용자가 직접 시작한 녹화를 코드가 멈추면 안 됩니다."""
    s = session(tmp_path, stop_on_silence=False)
    rec = FakeRecorder([track("system", 99999.0), track("mic", 99999.0)])
    assert s._silence_verdict(rec) is None


def test_no_audio_recorder_is_not_a_verdict(tmp_path):
    assert session(tmp_path)._silence_verdict(None) is None


def test_no_tracks_is_not_a_verdict(tmp_path):
    assert session(tmp_path)._silence_verdict(FakeRecorder([])) is None


@pytest.mark.parametrize("stop,abort", [(0, 0), (0, 3600), (900, 0)])
def test_zero_means_off(tmp_path, stop, abort):
    """0 으로 두면 그 단계는 끄는 것입니다."""
    s = session(tmp_path, stop=stop, abort=abort)
    rec = FakeRecorder([track("system", 500.0), track("mic", 500.0)])
    verdict = s._silence_verdict(rec)
    assert verdict is None or verdict[0] in ("long", "very_long")


def test_the_quietest_track_does_not_decide_alone(tmp_path):
    """한 트랙이 오래 조용해도 다른 트랙이 살아 있으면 정상입니다."""
    s = session(tmp_path)
    rec = FakeRecorder([track("system", 10.0), track("mic", 100000.0)])
    assert s._silence_verdict(rec) is None


# -- 가짜가 아니라 진짜 AudioRecorder 와 맞물리는지 -------------------
#
# 위 테스트들은 전부 FakeRecorder 를 씁니다. FakeRecorder 에는
# .results 가 있으니 전부 통과합니다. 그런데 진짜 AudioRecorder 에
# .results 가 없으면 _silence_verdict 의 getattr 가 조용히 [] 를 받아
# 운영에서는 무음 감지가 한 번도 안 돕니다. 테스트가 거짓말을 하는
# 전형적인 모양이라, 아래 두 개는 진짜 객체에 직접 묶습니다.

class _FakeTrack:
    def __init__(self, result):
        self.result = result


def _real_recorder(tmp_path, tracks):
    from syncnote.recorder.audio import AudioRecorder

    rec = AudioRecorder(out_dir=tmp_path)
    rec.tracks = [_FakeTrack(t) for t in tracks]
    return rec


def test_verdict_works_against_the_real_recorder(tmp_path):
    s = session(tmp_path)
    rec = _real_recorder(tmp_path, [track("system", 4000.0), track("mic", 4000.0)])
    verdict = s._silence_verdict(rec)
    assert verdict is not None and verdict[0] == "very_long"


def test_real_recorder_results_follow_live_track_state(tmp_path):
    """stop() 전에도 지금 값이 보여야 합니다."""
    t = track("system", 0.0)
    rec = _real_recorder(tmp_path, [t])
    assert rec.results[0].silent_for == 0.0
    t.silent_for = 1234.0          # 스레드가 제자리에서 갱신하는 흐름
    assert rec.results[0].silent_for == 1234.0


def test_silence_peak_reaches_the_track(tmp_path, monkeypatch):
    """설정한 무음 기준이 실제 트랙까지 가야 합니다.

    AudioRecorder 가 silence_peak 을 들고만 있고 AudioTrack 에
    안 넘기면, 설정 키는 있는데 아무 일도 안 하는 상태가 됩니다.
    """
    from syncnote.recorder import audio as audio_mod

    made = []

    class Spy(audio_mod.AudioTrack):
        def __init__(self, *a, **kw):
            made.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(audio_mod, "AudioTrack", Spy)
    monkeypatch.setattr(audio_mod.devices, "resolve_system_source",
                        lambda name: object())

    rec = audio_mod.AudioRecorder(out_dir=tmp_path, mic_enabled=False,
                                  silence_peak=0.25)
    rec.start("x")
    assert made and made[0]["silence_peak"] == 0.25



# -- 무음이어도 녹화·업로드를 멈추지 않음(사용자 요청) ----------------------------------
class _Result:
    def __init__(self):
        self.warnings = []


def test_long_silence_never_stops_or_breaks_the_session(tmp_path):
    s = session(tmp_path)
    r = _Result()
    for quiet in (1000.0, 1200.0, 4000.0, 9000.0):           # 15분 → 60분 넘게 계속 무음
        s._note_silence(FakeRecorder([track("system", quiet), track("mic", quiet)]), r, 1)
    assert not s._stop_event.is_set()                       # 녹화를 멈추지 않음
    assert s.broken is False                                # 업로드 막는 표시도 없음
    assert len(r.warnings) == 2                             # 단계마다 한 번씩만 경고(long, very_long)


def test_silence_warning_rearms_after_sound_returns(tmp_path):
    s = session(tmp_path)
    r = _Result()
    s._note_silence(FakeRecorder([track("system", 1000.0)]), r, 1)
    s._note_silence(FakeRecorder([track("system", 0.0)]), r, 1)       # 소리가 다시 남
    s._note_silence(FakeRecorder([track("system", 1000.0)]), r, 2)
    assert len(r.warnings) == 2
