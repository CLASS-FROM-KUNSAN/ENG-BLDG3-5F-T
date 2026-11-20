"""Silero VAD (ONNX) 감지기 및 정규화 단위 테스트.

onnxruntime 유무와 관계없이 FakeInferenceSession 모의 객체로 안전하게 검증합니다.
"""
from __future__ import annotations

import numpy as np

from syncnote.recorder import vad


class FakeInferenceSession:
    """ONNX InferenceSession 을 흉내 내는 가상 세션."""

    def __init__(self, speech_prob: float = 0.8) -> None:
        self.speech_prob = speech_prob
        self.first_state: np.ndarray | None = None
        self.last_input: np.ndarray | None = None
        self.last_state: np.ndarray | None = None

    def run(self, output_names, input_feed):
        if self.first_state is None:
            self.first_state = input_feed["state"].copy()
        self.last_input = input_feed["input"]
        self.last_state = input_feed["state"].copy()
        # 입력의 절대값 평균이 0.01 이하이면 정상 소음/무음으로 간주하여 낮은 확률 반환
        mean_amp = float(np.mean(np.abs(self.last_input)))
        prob = self.speech_prob if mean_amp > 0.01 else 0.05
        return [np.array([[prob]], dtype=np.float32)], np.ones_like(input_feed["state"])


def test_graceful_fallback_when_onnx_or_model_missing(monkeypatch, tmp_path):
    """ONNX 런타임이나 모델 파일이 없으면 예외 없이 None 을 반환하고 비활성화되어야 합니다."""
    monkeypatch.setattr(vad, "_SESSION", None)
    session = vad.get_vad_session(tmp_path / "non_existent_silero.onnx")
    assert session is None

    detector = vad.SileroVADDetector(model_path=tmp_path / "non_existent.onnx")
    assert detector.available is False
    dummy_audio = np.zeros(48000, dtype=np.float32)
    assert detector.is_speech_present(dummy_audio) is False


def test_int16_audio_is_normalized_to_float32(monkeypatch):
    """int16 배열이 들어오면 [-1.0, 1.0] 범위의 float32 로 정규화되어 VAD 모델에 전달되어야 합니다."""
    fake_session = FakeInferenceSession(speech_prob=0.9)
    monkeypatch.setattr(vad, "_SESSION", fake_session)

    detector = vad.SileroVADDetector()
    detector.session = fake_session

    # 48kHz, 1초, int16 사인파 (진폭 16384 -> 약 0.5)
    t = np.arange(48000) / 48000.0
    int16_audio = (16384 * np.sin(2 * np.pi * 440 * t)).astype(np.int16)

    result = detector.is_speech_present(int16_audio, orig_sr=48000)

    assert result is True
    assert fake_session.last_input is not None
    assert fake_session.last_input.dtype == np.float32
    assert np.max(np.abs(fake_session.last_input)) <= 1.0
    assert np.max(np.abs(fake_session.last_input)) < 1.0


def test_state_is_reset_on_each_check(monkeypatch):
    """새 버퍼 판정 시 이전 세그먼트의 은닉 상태(self._state)가 누적되지 않도록 reset 되어야 합니다."""
    fake_session = FakeInferenceSession()
    detector = vad.SileroVADDetector()
    detector.session = fake_session
    detector._state = np.ones((2, 1, 128), dtype=np.float32) * 99.0  # 이전 세그먼트의 오염 상태

    dummy_audio = np.ones(48000, dtype=np.float32) * 0.1
    detector.is_speech_present(dummy_audio, orig_sr=48000)

    # 메서드 시작 시점에 reset() 되어 초기 상태(0.0)가 첫 청크의 입력으로 전달되었는지 확인
    assert np.all(fake_session.first_state == 0.0)


def test_stationary_low_noise_is_not_speech(monkeypatch):
    """에어컨/팬 소음 같은 미세 정상 소음(진폭 < 0.01)은 음성으로 오판하지 않아야 합니다."""
    fake_session = FakeInferenceSession(speech_prob=0.9)
    detector = vad.SileroVADDetector()
    detector.session = fake_session

    # 미세 백색 소음 (진폭 0.0005)
    noise = np.random.uniform(-0.0005, 0.0005, 48000).astype(np.float32)
    assert detector.is_speech_present(noise, orig_sr=48000) is False
