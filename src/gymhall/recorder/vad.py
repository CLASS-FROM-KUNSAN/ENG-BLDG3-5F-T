"""Silero VAD (ONNX Runtime) 기반 음성 활동 감지기.

48kHz 스테레오 입력을 16kHz 모노로 초고속 다운샘플링하고,
512 샘플(32ms) 청크 단위로 사람의 발화 확률을 계산합니다.
onnxruntime 이 없거나 모델 파일이 없으면 안전하게 None 을 반환하여 폴백합니다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from ..logging_setup import get as get_logger

log = get_logger("recorder.vad")

_SESSION: Any = None


def get_vad_session(model_path: str | Path | None = None) -> Any:
    """ONNX InferenceSession 싱글톤 인스턴스 반환.

    onnxruntime 패키지가 없거나 모델 파일이 없으면 None 을 반환합니다.
    """
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    try:
        import onnxruntime as ort
    except ImportError:
        return None

    env_path = os.environ.get("SILERO_VAD_PATH")
    path = Path(model_path or env_path or "models/silero_vad.onnx")
    if not path.exists():
        return None

    try:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        _SESSION = ort.InferenceSession(
            str(path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        return _SESSION
    except Exception as exc:
        log.warning("Silero VAD ONNX 세션을 생성하지 못했습니다: %s", exc)
        return None


class SileroVADDetector:
    """Silero VAD v5 ONNX 모델 추론 래퍼."""

    def __init__(self, model_path: str | Path | None = None, threshold: float = 0.5) -> None:
        self.session = get_vad_session(model_path)
        self.threshold = float(threshold)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    @property
    def available(self) -> bool:
        return self.session is not None

    def reset(self) -> None:
        """은닉 상태(RNN/GRU state)를 0으로 초기화합니다."""
        self._state = np.zeros((2, 1, 128), dtype=np.float32)

    def is_speech_present(self, audio_data: np.ndarray, orig_sr: int = 48000) -> bool:
        """오디오 버퍼 전체에서 유의미한 사람 발화가 감지되는지 여부 반환."""
        if self.session is None or audio_data is None or audio_data.size == 0:
            return False

        # 0. 매 검사 시작 시 은닉 상태 초기화 (세그먼트 간 상태 누적 오염 방지)
        self.reset()

        # 1. 모노 변환
        audio_mono = np.mean(audio_data, axis=1) if audio_data.ndim > 1 else audio_data

        # 2. 16kHz 다운샘플링
        if orig_sr == 48000:
            audio_16k = audio_mono[::3]
        elif orig_sr == 16000:
            audio_16k = audio_mono
        else:
            step = max(1, round(orig_sr / 16000))
            audio_16k = audio_mono[::step]

        # 3. 데이터 타입 및 [-1.0, 1.0] 스케일 정규화
        if np.issubdtype(audio_16k.dtype, np.integer):
            audio_16k = audio_16k.astype(np.float32) / 32768.0
        else:
            audio_16k = audio_16k.astype(np.float32)

        # 4. 512 샘플(32ms @ 16kHz) 청크 단위 추론
        chunk_size = 512
        num_chunks = len(audio_16k) // chunk_size
        if num_chunks == 0:
            return False

        speech_chunks = 0
        for i in range(num_chunks):
            chunk = audio_16k[i * chunk_size : (i + 1) * chunk_size].reshape(1, -1)
            ort_inputs = {
                "input": chunk,
                "state": self._state,
                "sr": self._sr,
            }
            try:
                out, self._state = self.session.run(None, ort_inputs)
                prob = float(out[0].flat[0])
                if prob >= self.threshold:
                    speech_chunks += 1
            except Exception as exc:
                log.debug("VAD 추론 오류: %s", exc)
                return False

        # 최소 5청크(약 160ms) 이상 발화 감지 시 사람 음성이 존재하는 것으로 판단
        return speech_chunks >= 5
