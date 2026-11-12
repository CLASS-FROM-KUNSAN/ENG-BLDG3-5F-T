"""이 기기에서 실제로 관찰된 전사 속도를 기억합니다.

왜 필요한가
-----------
"small 모델이면 1시간 음성에 약 10분" 같은 숫자는 제가 지어낸 것이었습니다.
실제 소요 시간은 기기마다, 그리고 같은 기기에서도 그때의 CPU 경합에 따라
몇 배씩 차이 납니다(참고한 transcribe_app 은 같은 15분 오디오를 large-v3 로
8회 돌려 264.9초~1640.4초, 6배 넘는 편차를 실측했습니다).

그래서 추정하지 않고 측정합니다. 전사가 끝날 때마다 '오디오 1초를
처리하는 데 걸린 실제 시간'(RTF, real-time factor)을 지수이동평균으로
갱신하고, 다음부터는 그 값으로 예상 시간을 말합니다.

RTF 1.0 = 오디오 길이만큼 걸림. 0.2 = 5배 빠름. 2.0 = 두 배 걸림.

저사양 노트북에서는 이게 특히 중요합니다. 3시간짜리 녹화를 medium
모델로 돌리면 몇 시간이 걸리는지 '미리' 알 수 있어야 충전기를 꽂을지
모델을 낮출지 판단할 수 있습니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("transcribe.perf")

PROFILE_FILE = "perf_profile.json"

# 실측값이 하나도 없을 때의 출발점.
# CPU int8 기준의 대략적인 값이며, '작은 모델일수록 빠르다' 는 방향만
# 맞춘 것입니다. 첫 실행 뒤에는 관찰값으로 대체됩니다.
SEED_RTF_CPU = {
    "tiny": 0.05,
    "base": 0.09,
    "small": 0.22,
    "medium": 0.60,
    "large-v3": 1.15,
    "large-v2": 1.15,
    "large": 1.15,
}
DEFAULT_SEED = 0.5

# GPU 는 CPU 와 속도 차이가 커서 같은 통계에 섞으면 예상이 크게 틀어집니다.
GPU_SPEEDUP_GUESS = 1 / 12

# 최근 실행에 더 비중을 두되 과거도 반영합니다.
EMA_ALPHA = 0.3

# 말도 안 되는 값이 들어와 통계를 망치는 것을 막습니다.
# (중간에 멈춘 전사, 시계 변경 등)
MIN_SANE_RTF = 0.001
MAX_SANE_RTF = 100.0


def _key(model: str, device: str) -> str:
    return f"{device}:{model}"


def seed_rtf(model: str, device: str) -> float:
    base = SEED_RTF_CPU.get(model, DEFAULT_SEED)
    return base * GPU_SPEEDUP_GUESS if device == "cuda" else base


@dataclass
class Sample:
    rtf: float
    samples: int
    last_rtf: float


class Profile:
    """작은 JSON 한 개. 기기마다 따로 쌓입니다."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            log.warning("속도 기록이 손상돼 새로 시작합니다: %s", self.path)
            self.data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as exc:
            # 예상 시간 표시가 안 될 뿐, 전사 자체에는 지장이 없습니다.
            log.debug("속도 기록 저장 실패(무시): %s", exc)

    # -- 조회 --------------------------------------------------------
    def get(self, model: str, device: str) -> Sample | None:
        entry = self.data.get(_key(model, device))
        if not entry or not entry.get("rtf"):
            return None
        return Sample(
            rtf=float(entry["rtf"]),
            samples=int(entry.get("samples", 0)),
            last_rtf=float(entry.get("last_rtf", entry["rtf"])),
        )

    def rtf(self, model: str, device: str) -> float:
        found = self.get(model, device)
        return found.rtf if found else seed_rtf(model, device)

    def is_measured(self, model: str, device: str) -> bool:
        return self.get(model, device) is not None

    def estimate_seconds(self, audio_seconds: float, model: str, device: str) -> float:
        return max(1.0, float(audio_seconds) * self.rtf(model, device))

    # -- 기록 --------------------------------------------------------
    def record(self, audio_seconds: float, elapsed_seconds: float,
               model: str, device: str) -> float | None:
        """관찰값으로 갱신하고 새 평균을 돌려줍니다. 이상한 값은 무시합니다."""
        if audio_seconds <= 0 or elapsed_seconds <= 0:
            return None
        observed = elapsed_seconds / audio_seconds
        if not (MIN_SANE_RTF <= observed <= MAX_SANE_RTF):
            log.debug("비정상 RTF(%.4f) 무시", observed)
            return None

        key = _key(model, device)
        entry = self.data.get(key) or {}
        previous = entry.get("rtf")
        new = observed if previous is None else EMA_ALPHA * observed + (1 - EMA_ALPHA) * float(previous)

        self.data[key] = {
            "rtf": new,
            "samples": int(entry.get("samples", 0)) + 1,
            "last_rtf": observed,
        }
        self.save()
        log.info(
            "속도 기록: %s 실측 %.2fx (오디오 %.0f초 / 처리 %.0f초) -> 평균 %.2fx",
            key, observed, audio_seconds, elapsed_seconds, new,
        )
        return new


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}초"
    if seconds < 3600:
        return f"{seconds / 60:.0f}분"
    hours, rest = divmod(seconds, 3600)
    minutes = rest / 60
    return f"{hours:.0f}시간 {minutes:.0f}분" if minutes >= 1 else f"{hours:.0f}시간"


def describe_estimate(profile: Profile, audio_seconds: float,
                      model: str, device: str) -> str:
    """사용자에게 보여줄 한 줄. 추정인지 실측인지 분명히 밝힙니다."""
    estimate = profile.estimate_seconds(audio_seconds, model, device)
    found = profile.get(model, device)
    basis = f"이 기기 실측 {found.samples}회 기준" if found else "아직 실측 전 — 대략값"
    return f"예상 {human_duration(estimate)} ({basis}, {model}/{device})"
