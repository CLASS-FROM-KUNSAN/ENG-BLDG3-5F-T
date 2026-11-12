"""Whisper 가 지어낸 문장을 걸러냅니다.

왜 필요한가
-----------
Whisper 는 유튜브 자막으로 학습돼서, 무음이나 잡음 구간을 만나면
자막 상투어를 지어냅니다. 강의 녹화는 교수가 판서하는 시간, 학생이
생각하는 시간, 쉬는 시간처럼 무음이 길어서 이게 특히 잘 생깁니다.
걸러내지 않으면 전사본 곳곳에 "시청해주셔서 감사합니다" 가 박힙니다.

두 가지 실패 양상을 다룹니다.
  1) 상투어 환각 — 알려진 문구가 통째로 튀어나옴
  2) 반복 루프 — 같은 문장을 수십 번 되풀이함
     (condition_on_previous_text=True 일 때 특히 잘 생깁니다)

판단 근거
  - 알려진 상투어 목록과 정확히 일치
  - 모델 자신이 매긴 no_speech_prob 이 임계값을 넘음
  - 같은 문장이 연속으로 임계 횟수를 넘김

참고: 이 목록은 한국어·영어 유튜브 자막에서 흔한 문구입니다.
실제 강의에서 교수가 이 문장을 그대로 말할 확률은 사실상 없습니다.
그래도 오탐이 걱정되면 config 에서 끌 수 있습니다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..logging_setup import get as get_logger

log = get_logger("transcribe.cleanup")

# 무음 구간에서 지어내는 것으로 잘 알려진 문구들
HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    # 한국어 (유튜브 자막 상투어)
    "시청해주셔서 감사합니다",
    "시청해 주셔서 감사합니다",
    "끝까지 시청해주셔서 감사합니다",
    "구독과 좋아요 부탁드립니다",
    "구독과 좋아요",
    "구독 좋아요 알림설정",
    "구독과 좋아요 알림설정 부탁드립니다",
    "다음 영상에서 만나요",
    "다음 영상에서 뵙겠습니다",
    "많은 시청 부탁드립니다",
    "영상 끝까지 시청해주셔서 감사합니다",
    "한글자막 by 한효정",
    "이 영상은 유료광고를 포함하고 있습니다",
    "mbc 뉴스 이덕영입니다",
    # 영어
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "don't forget to subscribe",
    "like and subscribe",
    "see you in the next video",
    "subtitles by the amara.org community",
    "transcription by castingwords",
})

# 일부러 넣지 않은 것들
#   "thank you", "bye", "okay", "yes" 같은 짧은 말은 환각으로도 나오지만
#   실제 강의에서도 그대로 나옵니다. 특히 영어 강의라면 교수가 수업을
#   마치며 "Thank you." 라고 말합니다. 그걸 지우면 전사본에서 수업의
#   끝이 사라집니다. 짧은 말은 문구 규칙 대신 no_speech_prob 와
#   avg_logprob 로 거릅니다(아래 참고).

# 모델 스스로 '거의 무음' 이라고 본 구간. 이 값을 넘으면 텍스트를 믿지 않습니다.
DEFAULT_NO_SPEECH_THRESHOLD = 0.85

# OpenAI Whisper 자신의 기본 규칙: no_speech_prob 가 0.6 을 넘으면서
# avg_logprob 가 -1.0 보다 낮으면 무음으로 봅니다. 0.85 하나만 보면
# 0.6~0.85 구간에서 모델이 확신도 없이 지어낸 문장을 놓칩니다.
# 두 신호가 함께 나쁠 때만 적용하므로 단독 임계값보다 오탐이 적습니다.
WHISPER_NO_SPEECH_THRESHOLD = 0.6
WHISPER_LOGPROB_THRESHOLD = -1.0

# 같은 문장이 연속으로 이 횟수를 넘으면 루프로 봅니다.
DEFAULT_MAX_REPEATS = 3

_PUNCT = re.compile(r"[\s.,!?~…·\-'\"“”‘’()\[\]]+")


def normalize(text: str) -> str:
    """비교용 정규화. 문장부호와 공백을 걷어내고 소문자로 만듭니다."""
    text = unicodedata.normalize("NFC", text or "")
    return _PUNCT.sub(" ", text).strip().lower()


@dataclass
class CleanupStats:
    kept: int = 0
    dropped_phrase: int = 0
    dropped_silence: int = 0
    dropped_repeat: int = 0
    samples: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return self.dropped_phrase + self.dropped_silence + self.dropped_repeat

    def line(self) -> str:
        if not self.dropped:
            return f"{self.kept}개 구간, 제거 없음"
        return (
            f"{self.kept}개 구간 유지, {self.dropped}개 제거 "
            f"(상투어 {self.dropped_phrase} / 무음 {self.dropped_silence} / 반복 {self.dropped_repeat})"
        )


class Cleaner:
    """세그먼트를 순서대로 흘려 넣으면 버릴 것을 알려 줍니다."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        drop_phrases: bool = True,
        no_speech_threshold: float = DEFAULT_NO_SPEECH_THRESHOLD,
        max_repeats: int = DEFAULT_MAX_REPEATS,
        extra_phrases: tuple[str, ...] = (),
        use_logprob_rule: bool = True,
    ) -> None:
        self.enabled = enabled
        self.drop_phrases = drop_phrases
        self.no_speech_threshold = float(no_speech_threshold)
        self.max_repeats = max(0, int(max_repeats))
        self.use_logprob_rule = bool(use_logprob_rule)
        self.phrases = {normalize(p) for p in HALLUCINATION_PHRASES}
        self.phrases |= {normalize(p) for p in extra_phrases if p}
        self.phrases.discard("")
        self.stats = CleanupStats()
        self._last: str | None = None
        self._run = 0

    def should_drop(
        self,
        text: str,
        no_speech_prob: float | None = None,
        avg_logprob: float | None = None,
    ) -> tuple[bool, str]:
        """(버릴지, 이유). 이유는 로그에만 쓰입니다.

        엔진이 주지 않는 신호(None)는 그 규칙만 건너뜁니다.
        """
        stripped = (text or "").strip()
        if not stripped:
            return True, "빈 구간"

        if not self.enabled:
            self._note_kept(stripped)
            return False, ""

        norm = normalize(stripped)

        # 1) 상투어
        if self.drop_phrases and norm in self.phrases:
            self.stats.dropped_phrase += 1
            self._remember(stripped)
            return True, "상투어 환각"

        # 2) 모델이 스스로 무음이라고 본 구간
        if (
            no_speech_prob is not None
            and self.no_speech_threshold > 0
            and no_speech_prob > self.no_speech_threshold
        ):
            self.stats.dropped_silence += 1
            self._remember(stripped)
            return True, f"무음 추정(no_speech_prob={no_speech_prob:.2f})"

        # 2-b) 무음 의심 + 확신 없음 (Whisper 자체 기본 규칙)
        if (
            self.use_logprob_rule
            and no_speech_prob is not None
            and avg_logprob is not None
            and no_speech_prob > WHISPER_NO_SPEECH_THRESHOLD
            and avg_logprob < WHISPER_LOGPROB_THRESHOLD
        ):
            self.stats.dropped_silence += 1
            self._remember(stripped)
            return True, (
                f"무음 의심+저확신(no_speech_prob={no_speech_prob:.2f}, "
                f"avg_logprob={avg_logprob:.2f})"
            )

        # 3) 반복 루프
        if self.max_repeats:
            if norm == self._last:
                self._run += 1
            else:
                self._last, self._run = norm, 1
            if self._run > self.max_repeats:
                self.stats.dropped_repeat += 1
                self._remember(stripped)
                return True, f"같은 문장 {self._run}회 반복"
        self._note_kept(stripped)
        return False, ""

    # -- 내부 -------------------------------------------------------
    def _note_kept(self, text: str) -> None:
        self.stats.kept += 1

    def _remember(self, text: str) -> None:
        if len(self.stats.samples) < 5:
            self.stats.samples.append(text[:60])

    def report(self, where: str = "") -> None:
        """끝나고 한 번 호출해 결과를 로그에 남깁니다."""
        if not self.stats.dropped:
            return
        prefix = f"{where}: " if where else ""
        log.info("%s전사 정리 — %s", prefix, self.stats.line())
        for sample in self.stats.samples:
            log.debug("  제거된 예: %s", sample)


def from_config(cfg) -> Cleaner:
    return Cleaner(
        enabled=bool(cfg.get("transcribe.cleanup.enabled", True)),
        drop_phrases=bool(cfg.get("transcribe.cleanup.drop_hallucinations", True)),
        no_speech_threshold=float(
            cfg.get("transcribe.cleanup.no_speech_threshold", DEFAULT_NO_SPEECH_THRESHOLD)
        ),
        max_repeats=int(cfg.get("transcribe.cleanup.max_repeats", DEFAULT_MAX_REPEATS)),
        extra_phrases=tuple(cfg.get("transcribe.cleanup.extra_phrases", [])),
        use_logprob_rule=bool(cfg.get("transcribe.cleanup.use_logprob_rule", True)),
    )
