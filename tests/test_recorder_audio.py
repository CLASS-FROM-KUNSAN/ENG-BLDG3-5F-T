"""오디오 캡처 스레드.

실제 오디오 장치 없이 가짜 소스로 돌립니다.
"""

from __future__ import annotations

import time
import wave

import pytest

np = pytest.importorskip("numpy")

# numpy 가 있을 때만 의미가 있는 임포트입니다.

from syncnote.recorder.audio import AudioRecorder, AudioTrack  # noqa: E402


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
    from syncnote.transcribe.engine import EngineSpec
    from syncnote.transcribe.realtime import RealtimeTranscriber

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
    from syncnote.recorder import audio as audio_mod

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


# ---------------------------------------------------------------------------
# GPU 판정
# ---------------------------------------------------------------------------
def test_cpu_request_is_honoured():
    from syncnote.transcribe.engine import pick_device

    assert pick_device("cpu") == ("cpu", "int8")


def test_auto_falls_back_to_cpu_without_cuda(monkeypatch):
    from syncnote.transcribe import engine

    monkeypatch.setattr(engine, "probe_cuda", lambda: (False, None, "CUDA 없음"))
    assert engine.pick_device("auto") == ("cpu", "int8")


def test_small_vram_gpu_is_rejected(monkeypatch):
    """회귀 방지: 존재 여부만 보고 cuda 를 고르면 2GB 내장 GPU 에서
    large 모델 로딩 중 OOM 으로 죽습니다."""
    from syncnote.transcribe import engine

    calls = {"count": 1, "vram": 2.0}

    class FakeCT2:
        @staticmethod
        def get_cuda_device_count():
            return calls["count"]

        @staticmethod
        def get_supported_compute_types(device):
            return {"float32"}

    class FakeProps:
        total_memory = int(calls["vram"] * 1024**3)

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_properties(i):
            return FakeProps()

    class FakeTorch:
        cuda = FakeCuda()

    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", FakeCT2)
    monkeypatch.setitem(__import__("sys").modules, "torch", FakeTorch)

    ok, vram, message = engine.probe_cuda()
    assert ok is False
    assert vram == pytest.approx(2.0)
    assert "VRAM" in message
    assert engine.pick_device("auto") == ("cpu", "int8")


def test_compute_type_is_asked_not_hardcoded(monkeypatch):
    """회귀 방지: float16 을 하드코딩하면 지원하지 않는 카드에서
    모델 로딩이 실패합니다. ctranslate2 에 직접 물어봅니다."""
    from syncnote.transcribe import engine

    class FakeCT2:
        @staticmethod
        def get_supported_compute_types(device):
            return {"float32", "int8"}   # float16 없음

    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", FakeCT2)
    assert engine.cuda_compute_type() == "float32"


def test_compute_type_prefers_float16_when_available(monkeypatch):
    from syncnote.transcribe import engine

    class FakeCT2:
        @staticmethod
        def get_supported_compute_types(device):
            return {"float32", "float16", "int8_float16"}

    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", FakeCT2)
    assert engine.cuda_compute_type() == "float16"


def test_compute_type_survives_probe_failure(monkeypatch):
    from syncnote.transcribe import engine

    class Broken:
        @staticmethod
        def get_supported_compute_types(device):
            raise RuntimeError("드라이버 오류")

    monkeypatch.setitem(__import__("sys").modules, "ctranslate2", Broken)
    assert engine.cuda_compute_type() == "float32"


# --- 어느 장치를 실제로 잡았는지 ---------------------------------------
# 2026-11-13 Windows 실기: 두 트랙 모두 "전 구간 무음" 이었습니다.
# 기본 마이크가 '마이크 배열(Audio Share)' 라는 가상 장치로 잡혀
# 있었는데, 로그에는 장치 이름이 한 번도 안 나와서 그걸 알 수
# 없었습니다. 설정값이 아니라 **실제로 열린 것**을 적어야 합니다.
class _Src:
    def __init__(self, name):
        self.name = name


def test_the_opened_device_name_is_recorded():
    from syncnote.recorder.audio import _source_name

    assert _source_name(_Src("스피커 (Realtek)"), "") == "스피커 (Realtek)"


def test_a_mismatch_between_config_and_reality_shows_both():
    """'Focusrite' 라고 적었는데 다른 게 열렸으면 둘 다 보여야 합니다."""
    from syncnote.recorder.audio import _source_name

    got = _source_name(_Src("마이크 배열 (Audio Share)"), "Focusrite")
    assert "마이크 배열 (Audio Share)" in got and "Focusrite" in got


def test_a_matching_config_is_not_repeated():
    from syncnote.recorder.audio import _source_name

    got = _source_name(_Src("마이크 배열 (Audio Share)"), "Audio Share")
    assert got == "마이크 배열 (Audio Share)"


def test_a_nameless_source_falls_back_to_the_config():
    from syncnote.recorder.audio import _source_name

    assert _source_name(object(), "Focusrite") == "Focusrite"
    assert _source_name(object(), "") == "(기본 장치)"


def test_a_silent_track_names_the_device_it_used(tmp_path, caplog):
    """'전 구간 무음' 만으로는 어디를 고칠지 알 수 없습니다.

    진짜로 돌립니다. 로그 문구를 테스트가 다시 쓰면, 프로덕션에서
    그 줄을 지워도 초록입니다.
    """
    from syncnote.recorder.audio import AudioTrack

    track = AudioTrack(
        "mic", FakeSource(level=0.0), tmp_path / "m.wav",
        device_label="마이크 배열 (Audio Share)",
    )
    with caplog.at_level("WARNING", logger="syncnote"):
        track.start()
        time.sleep(0.5)
        track.stop()
        track.join(timeout=15)

    assert track.result.silent, "무음으로 판정되지 않았습니다"
    assert "마이크 배열 (Audio Share)" in caplog.text, caplog.text
    assert "mic_device" in caplog.text, "어느 설정을 고쳐야 하는지 안 알려 줍니다"


def test_a_track_with_sound_does_not_nag(tmp_path, caplog):
    from syncnote.recorder.audio import AudioTrack

    track = AudioTrack("system", FakeSource(level=0.5), tmp_path / "s.wav",
                       device_label="스피커 (Realtek)")
    with caplog.at_level("WARNING", logger="syncnote"):
        track.start()
        time.sleep(0.5)
        track.stop()
        track.join(timeout=15)
    assert not track.result.silent
    assert "전 구간 무음" not in caplog.text


def test_the_device_label_reaches_the_track(tmp_path, monkeypatch):
    """AudioRecorder 가 리졸버 결과를 AudioTrack 까지 넘기는지.

    중간에서 멈춰도 아무도 안 터집니다 — silence_peak 때와 같은 자리입니다.
    """
    from syncnote.recorder import audio as audio_mod

    made = []

    class Spy(audio_mod.AudioTrack):
        def __init__(self, *a, **kw):
            made.append(kw)

        def start(self):
            pass

    monkeypatch.setattr(audio_mod, "AudioTrack", Spy)
    monkeypatch.setattr(audio_mod.devices, "resolve_mic_source",
                        lambda name: _Src("마이크 배열 (Audio Share)"))
    rec = audio_mod.AudioRecorder(out_dir=tmp_path, system_enabled=False,
                                  mic_device="Focusrite")
    rec.start("x")
    assert made, "트랙이 만들어지지 않았습니다"
    label = made[0]["device_label"]
    assert "마이크 배열 (Audio Share)" in label and "Focusrite" in label, label


# --- '조용했다' 와 '아무것도 안 왔다' ----------------------------------
# 2026-11-13 실기 녹음 파일: 두 트랙 모두 11.8MB 에 0 아닌 바이트가
# 하나도 없었습니다. 레벨 로그만 봐서는 둘을 구분할 수 없었습니다.
#   - system: WASAPI 루프백은 재생 중인 게 없으면 정확히 0. 정상.
#   - mic:    진짜 마이크는 조용한 방에서도 ADC 열잡음으로 ±1 LSB 는
#             흔들립니다. 정확히 0 이면 신호 경로가 없는 것입니다.
def test_a_mic_at_exactly_zero_is_called_out_as_a_dead_path(tmp_path, caplog):
    from syncnote.recorder.audio import AudioTrack

    track = AudioTrack("mic", FakeSource(level=0.0), tmp_path / "m.wav",
                       device_label="마이크 배열 (Audio Share)")
    with caplog.at_level("WARNING", logger="syncnote"):
        track.start()
        time.sleep(0.5)
        track.stop()
        track.join(timeout=15)

    assert track.result.peak == 0.0
    assert "정확히 0" in caplog.text, caplog.text
    assert "마이크 배열 (Audio Share)" in caplog.text


def test_a_silent_system_track_is_not_called_a_dead_path(tmp_path, caplog):
    """루프백은 재생 중인 게 없으면 정확히 0 입니다. 정상입니다."""
    from syncnote.recorder.audio import AudioTrack

    track = AudioTrack("system", FakeSource(level=0.0), tmp_path / "s.wav",
                       device_label="스피커 (Realtek)")
    with caplog.at_level("WARNING", logger="syncnote"):
        track.start()
        time.sleep(0.5)
        track.stop()
        track.join(timeout=15)

    assert track.result.silent, "무음 판정 자체는 되어야 합니다"
    assert "정확히 0" not in caplog.text, "새벽에 매번 오류가 뜹니다"


def test_a_very_quiet_mic_is_not_called_a_dead_path(tmp_path, caplog):
    """잡음이라도 들어오면 장치는 살아 있는 것입니다. 무음 경고까지만."""
    from syncnote.recorder.audio import AudioTrack

    track = AudioTrack("mic", FakeSource(level=1e-7), tmp_path / "m.wav",
                       device_label="인텔 마이크")
    with caplog.at_level("WARNING", logger="syncnote"):
        track.start()
        time.sleep(0.5)
        track.stop()
        track.join(timeout=15)

    assert track.result.silent, "임계값 아래라 무음이어야 합니다"
    assert track.result.peak > 0.0
    assert "정확히 0" not in caplog.text, caplog.text
