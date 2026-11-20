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

# 아주 짧은 세그먼트에 적용하는 더 낮은 무음 임계값.
#
# 왜 필요한가: 실측 기록(codyssey_final docs/research/speak-hallucination.md:116)
# 에 따르면 Groq Whisper 는 1초 무음에 " you" 를 no_speech_prob 0.70,
# avg_logprob -0.71 로 내놨고 "기존 필터 통과" 했습니다. 같은 입력에 OpenAI
# whisper-1 은 0.94 를 줘서(:117) 걸렸습니다. 즉 **같은 계열, 같은 입력인데
# 0.70~0.94 로 흔들리고 우리 임계값 0.85 가 그 사이를 가릅니다.**
#
# 긴 문장은 증거가 많아 0.85 로 충분하지만, 한두 단어짜리는 증거가 거의
# 없습니다. 그래서 짧은 세그먼트에만 더 낮은 문턱을 적용합니다.
# 교수가 "네" 한 마디 할 때는 no_speech_prob 가 낮게 나오므로 살아남습니다.
SHORT_SEGMENT_WORDS = 2
SHORT_SEGMENT_NO_SPEECH = 0.5

_PUNCT = re.compile(r"[\s.,!?~…·\-'\"“”‘’()\[\]]+")

# 표기형 환각 — 내용이 아니라 '상태를 적은 꼬리표'.
# Whisper 가 잡음 구간에서 자주 내놓습니다. 강의실은 프로젝터 팬, 판서,
# 키보드, 에어컨 소리가 계속 나므로 이게 특히 잘 생깁니다.
#
# 참고한 구현(codyssey_final speech_compare.py:17-35)은 "라틴 문자가 하나도
# 없으면 버린다" 는 규칙을 썼는데, 그대로 가져오면 **한국어 전사가 통째로
# 삭제됩니다.** 그래서 '문자나 숫자가 하나도 없을 때' 로 바꿨습니다.
_BRACKETED_ONLY = re.compile(r"^\W*(?:\[[^\]]*\]|\([^)]*\)|<[^>]*>)\W*$")
_PLACEHOLDER_WORDS = frozenset({
    "silence", "silent", "no speech", "no audio", "no voice", "blank audio",
    "inaudible", "unintelligible", "music", "applause", "laughter", "sound",
    "no_speech", "blank_audio",
    "음악", "박수", "웃음", "무음", "잡음", "침묵", "소리", "알아들을 수 없음",
})


def _has_alnum(text: str) -> bool:
    """문자나 숫자가 하나라도 있는지. 한글·한자·키릴 전부 포함됩니다."""
    return any(ch.isalnum() for ch in text)


def is_placeholder(text: str) -> bool:
    """'내용' 이 아니라 '상태 꼬리표' 인지."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not _has_alnum(stripped):
        return True                       # "...", "♪♪♪", "---", "ㅡㅡ"
    if _BRACKETED_ONLY.match(stripped):
        return True                       # "[음악]", "(웃음)", "[Music]"
    return normalize(stripped) in _PLACEHOLDER_WORDS


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
    dropped_placeholder: int = 0
    # 버린 세그먼트의 '숫자' 를 남깁니다. 본문만 남기면 나중에 임계값이
    # 맞는지 틀린지 판단할 자료가 없습니다.
    samples: list[dict] = field(default_factory=list)

    @property
    def dropped(self) -> int:
        return (self.dropped_phrase + self.dropped_silence
                + self.dropped_repeat + self.dropped_placeholder)

    @property
    def drop_ratio(self) -> float:
        total = self.kept + self.dropped
        return self.dropped / total if total else 0.0

    def line(self) -> str:
        if not self.dropped:
            return f"{self.kept}개 구간, 제거 없음"
        return (
            f"{self.kept}개 구간 유지, {self.dropped}개 제거 "
            f"(상투어 {self.dropped_phrase} / 무음 {self.dropped_silence} / "
            f"반복 {self.dropped_repeat} / 표기형 {self.dropped_placeholder})"
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
        drop_placeholders: bool = True,
        short_segment_no_speech: float = SHORT_SEGMENT_NO_SPEECH,
    ) -> None:
        self.enabled = enabled
        self.drop_phrases = drop_phrases
        self.no_speech_threshold = float(no_speech_threshold)
        self.max_repeats = max(0, int(max_repeats))
        self.use_logprob_rule = bool(use_logprob_rule)
        self.drop_placeholders = bool(drop_placeholders)
        self.short_segment_no_speech = float(short_segment_no_speech)
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

        # 0) 표기형 환각 — 내용이 아니라 상태 꼬리표
        if self.drop_placeholders and is_placeholder(stripped):
            self.stats.dropped_placeholder += 1
            self._remember(stripped, no_speech_prob, avg_logprob, "표기형")
            return True, "표기형 환각"

        norm = normalize(stripped)

        # 1) 상투어
        if self.drop_phrases and norm in self.phrases:
            self.stats.dropped_phrase += 1
            self._remember(stripped, no_speech_prob, avg_logprob, "상투어")
            return True, "상투어 환각"

        # 2) 모델이 스스로 무음이라고 본 구간
        if (
            no_speech_prob is not None
            and self.no_speech_threshold > 0
            and no_speech_prob > self.no_speech_threshold
        ):
            self.stats.dropped_silence += 1
            self._remember(stripped, no_speech_prob, avg_logprob, "무음")
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
            self._remember(stripped, no_speech_prob, avg_logprob, "무음+저확신")
            return True, (
                f"무음 의심+저확신(no_speech_prob={no_speech_prob:.2f}, "
                f"avg_logprob={avg_logprob:.2f})"
            )

        # 2-c) 짧은 세그먼트는 더 낮은 문턱
        # no_speech_threshold 를 0 으로 꺼 두었으면 이 검사도 같이 끕니다.
        # 사용자가 "무음 판정 쓰지 마" 라고 한 것이므로 일관되어야 합니다.
        if (
            self.no_speech_threshold > 0
            and self.short_segment_no_speech > 0
            and no_speech_prob is not None
            and no_speech_prob > self.short_segment_no_speech
            and len(normalize(stripped).split()) <= SHORT_SEGMENT_WORDS
        ):
            self.stats.dropped_silence += 1
            self._remember(stripped, no_speech_prob, avg_logprob, "짧은구간")
            return True, (
                f"짧은 구간 무음 추정(no_speech_prob={no_speech_prob:.2f}, "
                f"{len(normalize(stripped).split())}단어)"
            )

        # 3) 반복 루프
        if self.max_repeats:
            if norm == self._last:
                self._run += 1
            else:
                self._last, self._run = norm, 1
            if self._run > self.max_repeats:
                self.stats.dropped_repeat += 1
                self._remember(stripped, no_speech_prob, avg_logprob, "반복")
                return True, f"같은 문장 {self._run}회 반복"
        self._note_kept(stripped)
        return False, ""

    # -- 내부 -------------------------------------------------------
    def _note_kept(self, text: str) -> None:
        self.stats.kept += 1

    def _remember(self, text: str, no_speech_prob: float | None = None,
                  avg_logprob: float | None = None, reason: str = "") -> None:
        if len(self.stats.samples) < 20:
            self.stats.samples.append({
                "text": text[:80],
                "reason": reason,
                "no_speech_prob": round(no_speech_prob, 3) if no_speech_prob is not None else None,
                "avg_logprob": round(avg_logprob, 3) if avg_logprob is not None else None,
            })

    def report(self, where: str = "") -> None:
        """끝나고 한 번 호출해 결과를 로그에 남깁니다."""
        if not self.stats.dropped:
            return
        prefix = f"{where}: " if where else ""
        log.info("%s전사 정리 — %s", prefix, self.stats.line())
        for sample in self.stats.samples:
            log.debug(
                "  제거(%s) nsp=%s lp=%s: %s",
                sample.get("reason", ""), sample.get("no_speech_prob"),
                sample.get("avg_logprob"), sample.get("text", ""),
            )
        # 너무 많이 지웠으면 알려 줍니다. 환각이 남는 것보다
        # 교수의 실제 말이 조용히 사라지는 쪽이 더 나쁩니다.
        if self.stats.drop_ratio > 0.2 and self.stats.dropped > 5:
            log.warning(
                "%s구간의 %.0f%%를 제거했습니다. 정리 규칙이 과하게 작동했을 수 있습니다. "
                "원본이 궁금하면 config 에서 transcribe.cleanup.enabled = false 로 두고 "
                "다시 전사해 보세요.",
                prefix, self.stats.drop_ratio * 100,
            )


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
        drop_placeholders=bool(cfg.get("transcribe.cleanup.drop_placeholders", True)),
        short_segment_no_speech=float(
            cfg.get("transcribe.cleanup.short_segment_no_speech", SHORT_SEGMENT_NO_SPEECH)
        ),
    )
