"""faster-whisper 모델 로딩과 장치 선택.

모델은 처음 쓸 때 자동으로 내려받습니다(수백 MB ~ 수 GB).
네트워크가 없는 강의실에서 처음 돌리면 실패하므로,
설치 직후 한 번은 인터넷이 되는 곳에서 돌려 두세요.
  python -m gymhall transcribe warmup
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..logging_setup import get as get_logger

log = get_logger("transcribe.engine")

MODEL_NOTES = {
    "tiny": "가장 빠름. 실시간용. 한국어 정확도는 낮습니다.",
    "base": "실시간 하한선.",
    "small": "저사양 노트북의 균형점. 1시간 음성에 CPU 로 약 10분.",
    "medium": "정확하지만 CPU 로 1시간 음성에 30분 이상.",
    "large-v3": "최고 품질. GPU 없이는 권하지 않습니다.",
}


class FasterWhisperMissing(Exception):
    def __str__(self) -> str:
        return (
            "faster-whisper 가 설치되지 않았습니다.\n"
            "  pip install faster-whisper\n"
            "CPU 전용이면 이것만으로 충분합니다. NVIDIA GPU 를 쓰려면\n"
            "CUDA 12 와 cuDNN 9 가 따로 필요합니다."
        )


def pick_device(requested: str = "auto") -> tuple[str, str]:
    """(device, compute_type) 을 정합니다."""
    requested = (requested or "auto").lower()
    if requested == "cuda":
        return "cuda", "float16"
    if requested == "cpu":
        return "cpu", "int8"

    # auto
    try:
        import ctranslate2  # type: ignore
        if ctranslate2.get_cuda_device_count() > 0:
            log.info("CUDA 장치를 찾았습니다. GPU 로 전사합니다.")
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def pick_threads(requested: int = 0) -> int:
    if requested and requested > 0:
        return int(requested)
    cores = os.cpu_count() or 4
    # 전부 쓰면 녹화/다른 작업이 멈춘 것처럼 느려집니다.
    return max(1, cores - 1)


@dataclass
class EngineSpec:
    model: str = "small"
    device: str = "auto"
    compute_type: str = "int8"
    cpu_threads: int = 0

    def load(self):
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise FasterWhisperMissing() from exc

        device, auto_compute = pick_device(self.device)
        compute = self.compute_type if self.compute_type else auto_compute
        if device == "cpu" and compute in {"float16", "int8_float16"}:
            log.warning("CPU 에서는 %s 를 쓸 수 없어 int8 로 바꿉니다.", compute)
            compute = "int8"

        threads = pick_threads(self.cpu_threads)
        log.info(
            "모델 로딩: %s (device=%s, compute=%s, threads=%d) — %s",
            self.model, device, compute, threads, MODEL_NOTES.get(self.model, ""),
        )
        return WhisperModel(
            self.model, device=device, compute_type=compute,
            cpu_threads=threads, num_workers=1,
        )
