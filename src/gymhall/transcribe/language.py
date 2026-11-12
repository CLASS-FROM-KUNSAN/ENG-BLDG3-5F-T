"""전사 언어 결정.

왜 필요한가
-----------
`language = "ko"` 로 고정해 두면 영어 강의가 통째로 망가집니다. Whisper 는
지정된 언어로 '받아쓰려고' 하기 때문에, 영어 음성을 한국어로 지정하면
소리 나는 대로 한글을 뱉습니다("디스 이즈 어 바이너리 트리"). 수강 과목에
영어 수업이 하나라도 있으면 그 과목 전사본은 전부 쓸모없어집니다.

반대로 전부 "auto" 로 두는 것도 답이 아닙니다. 한국어 강의 중간의 영어
전문용어 구간에서 언어 판정이 흔들려 일본어·중국어로 오판하는 일이
흔합니다. 그래서 후보를 제한하는 방식이 가장 안전합니다.

세 가지 모드
  "ko" / "en" 처럼 하나   : 그 언어로 고정. 가장 정확합니다.
  "ko+en" 처럼 여럿       : 모델이 판정하되 후보 안에서만 고릅니다.
  "auto"                  : 모델에 전적으로 맡깁니다.

후보 제한은 seongbin45/transcribe_app 의 방식입니다. 모델이 준 전체
언어 확률에서 후보만 남겨 다시 정규화하면, 일본어/중국어 같은 엉뚱한
후보를 아예 배제할 수 있습니다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_setup import get as get_logger

log = get_logger("transcribe.language")

AUTO = "auto"

# 흔히 쓰는 표기를 Whisper 코드로 맞춥니다.
ALIASES = {
    "korean": "ko", "kor": "ko", "한국어": "ko",
    "english": "en", "eng": "en", "영어": "en",
    "japanese": "ja", "chinese": "zh",
}


def parse(value) -> tuple[str, ...]:
    """설정값을 후보 목록으로 바꿉니다.

    "ko"      -> ("ko",)
    "ko+en"   -> ("ko", "en")
    ["ko","en"] -> ("ko", "en")
    "auto"/"" -> ()           빈 목록은 '제한 없음' 을 뜻합니다.
    """
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value).replace(",", "+").split("+")

    out: list[str] = []
    for part in parts:
        code = part.strip().lower()
        if not code or code == AUTO:
            continue
        code = ALIASES.get(code, code)
        if code not in out:
            out.append(code)
    return tuple(out)


def whisper_argument(candidates: tuple[str, ...]) -> str | None:
    """faster-whisper 의 language 인자로 넘길 값.

    후보가 하나면 그 언어로 고정합니다(가장 정확). 여럿이거나 없으면
    None 을 넘겨 모델이 판정하게 하고, 결과는 restrict() 로 후보 안에
    가둡니다.
    """
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class Detected:
    code: str
    probability: float
    restricted: bool = False   # 후보 제한이 실제로 판정을 바꿨는지


def restrict(info, candidates: tuple[str, ...]) -> Detected:
    """모델의 언어 판정을 후보 안으로 가둡니다.

    info 는 faster-whisper 의 TranscriptionInfo 입니다
    (language, language_probability, all_language_probs).
    """
    detected = getattr(info, "language", "") or ""
    probability = float(getattr(info, "language_probability", 0.0) or 0.0)

    if not candidates:
        return Detected(detected, probability)
    if len(candidates) == 1:
        # 고정했으므로 판정이랄 게 없습니다.
        return Detected(candidates[0], 1.0)
    if detected in candidates:
        return Detected(detected, probability)

    all_probs = getattr(info, "all_language_probs", None)
    if not all_probs:
        # 전체 확률을 못 받으면 첫 후보로 떨어집니다.
        log.debug("언어 확률 분포가 없어 첫 후보(%s)를 씁니다.", candidates[0])
        return Detected(candidates[0], 0.0, restricted=True)

    kept = [(code, float(p)) for code, p in all_probs if code in candidates]
    total = sum(p for _, p in kept)
    if not kept or total <= 0:
        return Detected(candidates[0], 0.0, restricted=True)

    kept.sort(key=lambda x: -x[1])
    best_code, best_prob = kept[0]
    log.info(
        "언어 판정 %s -> %s 로 교정 (후보 %s 안에서 %.0f%%)",
        detected or "?", best_code, "+".join(candidates), best_prob / total * 100,
    )
    return Detected(best_code, best_prob / total, restricted=True)


def describe(candidates: tuple[str, ...]) -> str:
    if not candidates:
        return "자동 감지"
    if len(candidates) == 1:
        return f"{candidates[0]} 고정"
    return f"{'+'.join(candidates)} 중 자동 선택"
