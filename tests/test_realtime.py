"""실시간 전사 스레드.

faster-whisper 없이 가짜 모델로 전사 루프만 검증합니다.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

# numpy 가 있을 때만 의미가 있는 임포트입니다.

from gymhall.transcribe.engine import EngineSpec  # noqa: E402
from gymhall.transcribe.realtime import RealtimeTranscriber, maybe_start  # noqa: E402


class FakeSeg:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self, delay=0.0, text="안녕하세요"):
        self.delay = delay
        self.text = text
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return [FakeSeg(self.text)], None


def make(tmp_path, model, **kw):
    worker = RealtimeTranscriber(
        out_path=tmp_path / "rt.txt", spec=EngineSpec(), samplerate=48000,
        chunk_sec=kw.pop("chunk_sec", 0.2), overlap_sec=kw.pop("overlap_sec", 0.05),
        print_to_console=False, **kw,
    )
    worker.spec.load = lambda: model  # type: ignore[method-assign]
    return worker


def feed(worker, seconds=1.0, samplerate=48000):
    block = np.zeros((2048, 2), dtype=np.float32)
    sent = 0
    while sent < seconds * samplerate:
        try:
            worker.audio_queue.put(block, timeout=0.2)
        except queue.Full:
            pass
        sent += len(block)


def test_can_be_constructed():
    """회귀 방지: @dataclass 로 Thread 를 만들면 __hash__ 가 지워져
    Thread.__init__ 의 WeakSet 등록이 TypeError 로 실패합니다.
    즉 실시간 전사를 켜는 순간 생성부터 터집니다."""
    worker = RealtimeTranscriber(out_path=Path("/tmp/x.txt"), spec=EngineSpec())
    assert isinstance(worker, threading.Thread)
    assert hash(worker) is not None


def test_overlap_cannot_exceed_half_the_chunk():
    """overlap >= chunk 이면 버퍼가 줄지 않아 무한 루프가 됩니다."""
    worker = RealtimeTranscriber(
        out_path=Path("/tmp/x.txt"), spec=EngineSpec(), chunk_sec=4, overlap_sec=99
    )
    assert worker.overlap_sec <= 2.0


def test_writes_transcript_lines(tmp_path):
    model = FakeModel()
    worker = make(tmp_path, model)
    worker.start()
    feed(worker, seconds=1.0)
    time.sleep(0.5)
    worker.stop()
    worker.join(timeout=15)
    assert not worker.is_alive()

    text = worker.out_path.read_text(encoding="utf-8")
    assert "안녕하세요" in text
    assert "임시본" in text, "임시본이라는 경고가 파일에 없습니다"
    assert worker.lines, "전사 줄이 수집되지 않았습니다"
    assert re_timestamp(worker.lines[0]), worker.lines[0]


def re_timestamp(line: str) -> bool:
    import re

    return bool(re.match(r"^\[\d{2}:\d{2}\]", line))


def test_model_load_failure_disables_quietly(tmp_path):
    """모델이 없어도 녹화 전체를 죽이면 안 됩니다."""
    worker = make(tmp_path, None)
    worker.spec.load = lambda: (_ for _ in ()).throw(RuntimeError("모델 없음"))  # type: ignore
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()


def test_empty_transcription_writes_nothing(tmp_path):
    worker = make(tmp_path, FakeModel(text="   "))
    worker.start()
    feed(worker, seconds=0.8)
    time.sleep(0.4)
    worker.stop()
    worker.join(timeout=15)
    assert worker.lines == []


def test_slow_model_does_not_hang_shutdown(tmp_path):
    """전사가 입력보다 느려도 stop() 이 걸리면 끝나야 합니다."""
    worker = make(tmp_path, FakeModel(delay=0.3), chunk_sec=0.2)
    worker.start()
    feed(worker, seconds=0.6)
    worker.stop()
    started = time.monotonic()
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert time.monotonic() - started < 20


def test_transcribe_exception_does_not_kill_thread(tmp_path):
    class Exploding:
        def transcribe(self, audio, **kw):
            raise RuntimeError("추론 실패")

    worker = make(tmp_path, Exploding())
    worker.start()
    feed(worker, seconds=0.8)
    time.sleep(0.3)
    worker.stop()
    worker.join(timeout=15)
    assert not worker.is_alive()


def test_mono_input_accepted(tmp_path):
    worker = make(tmp_path, FakeModel())
    worker.start()
    for _ in range(20):
        worker.audio_queue.put(np.zeros(2048, dtype=np.float32))
    time.sleep(0.5)
    worker.stop()
    worker.join(timeout=15)
    assert worker.out_path.exists()


# ---------------------------------------------------------------------------
class FakeConfig:
    def __init__(self, **over):
        self.values = {
            "transcribe.enabled": True,
            "transcribe.realtime.enabled": False,
            "transcribe.realtime.model": "tiny",
            "transcribe.realtime.device": "cpu",
            "transcribe.realtime.compute_type": "int8",
            "transcribe.realtime.cpu_threads": 2,
            "transcribe.realtime.chunk_sec": 8,
            "transcribe.realtime.overlap_sec": 1.5,
            "transcribe.realtime.print_to_console": False,
            "transcribe.language": "ko",
            "recording.audio.samplerate": 48000,
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_maybe_start_returns_none_when_disabled(tmp_path):
    assert maybe_start(FakeConfig(), tmp_path, "x") is None


def test_maybe_start_returns_none_when_transcribe_off(tmp_path):
    cfg = FakeConfig(**{"transcribe.enabled": False, "transcribe.realtime.enabled": True})
    assert maybe_start(cfg, tmp_path, "x") is None


# ---------------------------------------------------------------------------
# 리샘플링
# ---------------------------------------------------------------------------
def test_resample_integer_ratio_averages_not_decimates(tmp_path):
    """48k -> 16k 는 3:1 입니다. chunk[::3] 로 뽑으면 8kHz 위 성분이
    되접혀 자음이 뭉개집니다. 평균을 내면 간단한 저역통과가 됩니다."""
    worker = RealtimeTranscriber(out_path=tmp_path / "x.txt", spec=EngineSpec(),
                                 samplerate=48000)
    # 20kHz 는 16kHz 샘플링의 나이퀴스트(8kHz)보다 훨씬 위입니다.
    # 단순 추출이면 4kHz 로 되접혀 그대로 큰 값이 남고(대조군),
    # 3샘플 평균은 저역통과 역할을 해 크게 줄어듭니다.
    n = np.arange(48000)
    tone = np.sin(2 * np.pi * 20000 * n / 48000).astype(np.float32)
    out = worker._resample_to_16k(tone)
    assert len(out) == 16000
    assert np.max(np.abs(out)) < 0.5, f"에일리어싱이 걸러지지 않았습니다: {np.max(np.abs(out)):.3f}"
    assert np.max(np.abs(tone[::3])) > 0.9, "대조군: 단순 추출은 그대로 통과해야 합니다"


def test_resample_preserves_speech_band(tmp_path):
    """사람 목소리 대역(1kHz)은 살아 있어야 합니다."""
    worker = RealtimeTranscriber(out_path=tmp_path / "x.txt", spec=EngineSpec(),
                                 samplerate=48000)
    n = np.arange(48000)
    tone = np.sin(2 * np.pi * 1000 * n / 48000).astype(np.float32)
    out = worker._resample_to_16k(tone)
    assert np.max(np.abs(out)) > 0.8


def test_resample_non_integer_ratio(tmp_path):
    """44.1k -> 16k 는 정수배가 아닙니다. 선형보간으로 맞춥니다."""
    worker = RealtimeTranscriber(out_path=tmp_path / "x.txt", spec=EngineSpec(),
                                 samplerate=44100)
    out = worker._resample_to_16k(np.zeros(44100, dtype=np.float32))
    assert abs(len(out) - 16000) <= 1


def test_resample_passthrough_at_16k(tmp_path):
    worker = RealtimeTranscriber(out_path=tmp_path / "x.txt", spec=EngineSpec(),
                                 samplerate=16000)
    data = np.ones(1000, dtype=np.float32)
    assert len(worker._resample_to_16k(data)) == 1000


def test_to_mono_handles_both_shapes():
    stereo = np.stack([np.ones(100), np.zeros(100)], axis=1).astype(np.float32)
    assert RealtimeTranscriber._to_mono(stereo).shape == (100,)
    assert np.allclose(RealtimeTranscriber._to_mono(stereo), 0.5)
    mono = np.ones(100, dtype=np.float32)
    assert RealtimeTranscriber._to_mono(mono).shape == (100,)


def test_tail_audio_is_not_discarded(tmp_path):
    """정지 신호가 와도 버퍼에 1초 이상 남아 있으면 전사해야 합니다.
    마지막 몇 초가 통째로 사라지면 결론부가 날아갑니다."""
    model = FakeModel()
    worker = make(tmp_path, model, chunk_sec=2.0, overlap_sec=0.1)
    worker.start()
    # chunk(2초)보다 짧은 1.5초만 넣고 바로 정지
    block = np.zeros((4800, 2), dtype=np.float32)
    for _ in range(15):
        worker.audio_queue.put(block)
    time.sleep(0.2)
    worker.stop()
    worker.join(timeout=20)
    assert model.calls >= 1, "꼬리 오디오가 버려졌습니다"
