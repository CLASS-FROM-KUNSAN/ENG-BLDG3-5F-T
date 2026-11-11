"""오디오 캡처 스레드.

실제 오디오 장치 없이 가짜 소스로 돌립니다.
"""

from __future__ import annotations

import time
import wave

import pytest

np = pytest.importorskip("numpy")

# numpy 가 있을 때만 의미가 있는 임포트입니다.

from gymhall.recorder.audio import AudioRecorder, AudioTrack  # noqa: E402


class FakeRecorder:
    def __init__(self, samplerate, channels, level=0.5, freq=440.0):
        self.sr = samplerate
        self.ch = channels
        self.level = level
        self.t = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes):
        # 실시간처럼 흐르게 해서 스레드 타이밍을 실제와 비슷하게 만듭니다.
        time.sleep(numframes / self.sr)
        n = np.arange(numframes) / self.sr + self.t
        self.t += numframes / self.sr
        wave_ = self.level * np.sin(2 * np.pi * 440 * n)
        return np.stack([wave_] * self.ch, axis=1).astype(np.float32)


class FakeSource:
    def __init__(self, level=0.5):
        self.level = level

    def recorder(self, samplerate, channels, blocksize):
        return FakeRecorder(samplerate, channels, self.level)


class BrokenSource:
    def recorder(self, samplerate, channels, blocksize):
        raise RuntimeError("장치가 사용 중입니다")


# ---------------------------------------------------------------------------
def _run_track(tmp_path, *, samplerate=48000, channels=2, level=0.5,
               gain_db=0.0, seconds=0.8, silence_warn_sec=60.0):
    track = AudioTrack(
        "system", FakeSource(level), tmp_path / "s.wav",
        samplerate=samplerate, channels=channels, gain_db=gain_db,
        silence_warn_sec=silence_warn_sec,
    )
    track.start()
    time.sleep(seconds)
    track.stop()
    track.join(timeout=15)
    assert not track.is_alive(), "스레드가 멈추지 않았습니다"
    return track.result


def test_track_join_does_not_explode(tmp_path):
    """회귀 방지: Thread 하위 클래스에서 `_stop` 이라는 이름을 쓰면
    Python 3.12 이하에서 threading.Thread._stop() 메서드를 덮어써
    join() 이 TypeError: 'Event' object is not callable 로 터집니다.
    3.13 에서는 그 메서드가 없어 증상이 숨겨지므로, 개발 환경에서는
    멀쩡하고 사용자 PC 에서만 녹화가 깨집니다.
    """
    result = _run_track(tmp_path)
    assert result.error is None
    assert result.frames > 0


# threading.Thread 가 역사적으로 가져온 **메서드** 이름들.
# 데이터 속성(_args, _target, _name ...)은 넣지 않습니다. 그건
# Thread.__init__ 이 인스턴스에 직접 넣는 값이라 겹치는 게 정상입니다.
# 위험한 것은 '메서드' 를 호출 불가능한 값으로 덮는 경우입니다.
#
# 현재 실행 중인 파이썬의 Thread 를 보고 판단하면 안 됩니다. 3.13 은
# `_stop` 을 없앴기 때문에, 3.13 에서만 보면 3.11/3.12 에서 터지는 코드가
# 그대로 통과합니다.
THREAD_METHODS = frozenset({
    "_stop", "_delete", "_bootstrap", "_bootstrap_inner", "_reset_internal_locks",
    "_set_ident", "_set_native_id", "_set_tstate_lock", "_wait_for_tstate_lock",
    "run", "start", "join", "is_alive", "isDaemon", "setDaemon", "getName", "setName",
})


def test_thread_subclasses_do_not_shadow_thread_internals(tmp_path):
    """회귀 방지: Thread 하위 클래스가 Thread 의 메서드를 값으로 덮으면
    join() 이 TypeError 로 터집니다(3.12 이하). 실행 중인 파이썬 버전과
    무관하게 잡아내도록 이름 목록을 고정해 두고 비교합니다.
    """
    from gymhall.transcribe.engine import EngineSpec
    from gymhall.transcribe.realtime import RealtimeTranscriber

    instances = [
        AudioTrack("system", FakeSource(), tmp_path / "a.wav"),
        RealtimeTranscriber(out_path=tmp_path / "r.txt", spec=EngineSpec()),
    ]
    for instance in instances:
        clashes = {
            name for name, value in vars(instance).items()
            if name in THREAD_METHODS and not callable(value)
        }
        assert not clashes, (
            f"{type(instance).__name__} 이 Thread 의 메서드를 값으로 덮습니다: "
            f"{sorted(clashes)}. 다른 이름을 쓰세요(예: _stop -> _stop_event)."
        )


def test_wav_header_matches_requested_samplerate(tmp_path):
    result = _run_track(tmp_path, samplerate=44100)
    with wave.open(str(result.path)) as w:
        assert w.getframerate() == 44100
        assert w.getnchannels() == 2
        assert w.getsampwidth() == 2  # int16


def test_reported_seconds_uses_real_samplerate(tmp_path):
    """48000 을 하드코딩하면 44100 설정에서 길이가 8% 틀어집니다."""
    result = _run_track(tmp_path, samplerate=44100)
    with wave.open(str(result.path)) as w:
        actual = w.getnframes() / w.getframerate()
    assert abs(result.seconds - actual) < 0.01


def test_mono_is_supported(tmp_path):
    result = _run_track(tmp_path, channels=1)
    with wave.open(str(result.path)) as w:
        assert w.getnchannels() == 1
    assert result.error is None


def test_silence_is_detected(tmp_path):
    """장치를 잘못 골라 3시간을 무음으로 녹음하는 사고를 막는 신호입니다."""
    result = _run_track(tmp_path, level=0.0)
    assert result.silent is True
    assert result.peak < 1e-4


def test_sound_is_not_flagged_silent(tmp_path):
    result = _run_track(tmp_path, level=0.5)
    assert result.silent is False
    assert result.peak > 0.4


def test_gain_is_applied(tmp_path):
    quiet = _run_track(tmp_path / "a", level=0.1, gain_db=0.0)
    loud = _run_track(tmp_path / "b", level=0.1, gain_db=12.0)
    assert loud.peak > quiet.peak * 3


def test_clipping_does_not_overflow(tmp_path):
    """게인을 과하게 올려도 int16 범위를 넘겨 깨지지 않아야 합니다."""
    result = _run_track(tmp_path, level=0.9, gain_db=24.0)
    assert result.error is None
    with wave.open(str(result.path)) as w:
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype="<i2")
    assert samples.min() >= -32768 and samples.max() <= 32767


def test_broken_device_is_reported_not_raised(tmp_path):
    """장치를 못 열면 녹화 전체를 죽이지 말고 기록만 남깁니다."""
    track = AudioTrack("mic", BrokenSource(), tmp_path / "m.wav")
    track.start()
    track.join(timeout=10)
    assert track.result.error is not None
    assert "사용 중" in track.result.error


def test_recorder_continues_when_one_device_fails(tmp_path, monkeypatch):
    """마이크가 없어도 시스템 소리는 녹음돼야 합니다."""
    from gymhall.recorder import audio as audio_mod

    monkeypatch.setattr(audio_mod.devices, "resolve_system_source",
                        lambda name="": FakeSource(0.5))
    monkeypatch.setattr(audio_mod.devices, "resolve_mic_source",
                        lambda name="": (_ for _ in ()).throw(RuntimeError("마이크 없음")))

    rec = AudioRecorder(out_dir=tmp_path, system_enabled=True, mic_enabled=True)
    rec.start("seg")
    time.sleep(0.5)
    results = rec.stop()
    assert len(results) == 1
    assert results[0].name == "system"
    assert results[0].error is None


def test_realtime_tap_receives_audio(tmp_path):
    """실시간 전사로 흘려보내는 큐가 채워지는지."""
    import queue

    tap: queue.Queue = queue.Queue(maxsize=50)
    track = AudioTrack("system", FakeSource(0.5), tmp_path / "s.wav", tap=tap)
    track.start()
    time.sleep(0.6)
    track.stop()
    track.join(timeout=10)
    assert tap.qsize() > 0


def test_full_tap_does_not_break_recording(tmp_path):
    """전사가 밀려 큐가 가득 차도 녹음은 계속돼야 합니다."""
    import queue

    tap: queue.Queue = queue.Queue(maxsize=1)
    track = AudioTrack("system", FakeSource(0.5), tmp_path / "s.wav", tap=tap)
    track.start()
    time.sleep(0.6)
    track.stop()
    track.join(timeout=10)
    assert track.result.error is None
    assert track.result.frames > 0
