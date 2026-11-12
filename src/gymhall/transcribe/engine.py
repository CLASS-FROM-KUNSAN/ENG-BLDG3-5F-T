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


# large-v3 를 GPU float16 으로 돌리는 데 대략 4.7GB + 여유가 필요합니다.
# 이보다 적은 VRAM 에서 cuda 를 고르면 로딩 도중 OOM 으로 죽습니다.
MIN_VRAM_GB = 5.0


def probe_cuda() -> tuple[bool, float | None, str]:
    """(쓸 수 있는가, VRAM GB, 사람이 읽을 설명).

    '있나 없나' 만 보면 안 됩니다. 2GB 짜리 내장 GPU 에서도
    get_cuda_device_count() 는 1 을 돌려주고, 그대로 large 모델을 올리면
    OOM 으로 죽습니다. 실제 VRAM 까지 확인합니다.
    """
    try:
        import ctranslate2  # type: ignore
    except ImportError:
        return False, None, "ctranslate2 가 없습니다(CPU 로 진행)."

    try:
        if ctranslate2.get_cuda_device_count() <= 0:
            return False, None, "CUDA 장치가 없습니다(CPU 로 진행)."
    except Exception as exc:
        return False, None, f"CUDA 조회 실패({exc}). CPU 로 진행합니다."

    vram: float | None = None
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        # torch 가 없어도 ctranslate2 만으로 전사는 됩니다. VRAM 만 모를 뿐입니다.
        pass

    if vram is not None and vram < MIN_VRAM_GB:
        return False, vram, (
            f"GPU 가 있지만 VRAM 이 부족합니다({vram:.1f}GB < {MIN_VRAM_GB:.0f}GB). "
            "CPU 로 진행합니다. 꼭 GPU 를 쓰려면 device=\"cuda\" 와 작은 모델을 지정하세요."
        )

    size = f"{vram:.1f}GB" if vram is not None else "용량 미확인"
    return True, vram, f"CUDA 사용 가능({size})."


def cuda_compute_type(preferred: str = "") -> str:
    """이 GPU 가 실제로 지원하는 연산 정밀도를 고릅니다.

    float16 을 하드코딩하면 안 됩니다. 오래된 카드는 지원하지 않아
    로딩 단계에서 실패합니다. ctranslate2 에 직접 물어봅니다.
    """
    try:
        import ctranslate2  # type: ignore

        supported = set(ctranslate2.get_supported_compute_types("cuda"))
    except Exception as exc:
        log.debug("지원 연산 정밀도 조회 실패(%s). float32 로 둡니다.", exc)
        return "float32"

    if preferred and preferred in supported:
        return preferred
    for candidate in ("float16", "int8_float16", "float32"):
        if candidate in supported:
            return candidate
    return "float32"


def pick_device(requested: str = "auto") -> tuple[str, str]:
    """(device, compute_type) 을 정합니다."""
    requested = (requested or "auto").lower()

    if requested == "cpu":
        return "cpu", "int8"

    if requested == "cuda":
        # 사용자가 명시했으면 존중하되, 정밀도는 실제 지원 목록에서 고릅니다.
        ok, vram, message = probe_cuda()
        if not ok:
            log.warning("device=\"cuda\" 로 지정했지만 %s", message)
        return "cuda", cuda_compute_type()

    ok, _vram, message = probe_cuda()
    if ok:
        log.info("%s GPU 로 전사합니다.", message)
        return "cuda", cuda_compute_type()
    log.info("%s", message)
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
