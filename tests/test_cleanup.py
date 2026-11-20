"""Whisper 환각 필터.

강의 녹화는 무음 구간이 길어 환각이 특히 잘 생깁니다.
걸러내지 않으면 전사본 곳곳에 유튜브 자막 상투어가 박힙니다.
"""

from __future__ import annotations

import pytest

from syncnote.transcribe.cleanup import (
    DEFAULT_NO_SPEECH_THRESHOLD,
    Cleaner,
    from_config,
    normalize,
)


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "a,b",
    [
        ("시청해주셔서 감사합니다", "시청해주셔서 감사합니다."),
        ("Thank you for watching", "thank you for watching!"),
        ("구독과  좋아요", "구독과 좋아요~"),
        ("Bye", "bye..."),
        ("“구독과 좋아요”", "구독과 좋아요"),
    ],
)
def test_normalize_ignores_punctuation_and_case(a, b):
    assert normalize(a) == normalize(b)


def test_normalize_keeps_real_difference():
    assert normalize("자료구조 수업") != normalize("운영체제 수업")


# ---------------------------------------------------------------------------
# 상투어
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phrase",
    [
        "시청해주셔서 감사합니다",
        "시청해주셔서 감사합니다.",
        "구독과 좋아요 부탁드립니다",
        "다음 영상에서 만나요",
        "Thank you for watching!",
        "Please subscribe",
        "Subtitles by the Amara.org community",
    ],
)
def test_known_hallucinations_are_dropped(phrase):
    drop, reason = Cleaner().should_drop(phrase, 0.1)
    assert drop is True
    assert "환각" in reason


@pytest.mark.parametrize(
    "text",
    [
        "오늘은 이진 탐색 트리를 배우겠습니다",
        "자 여기 보시면 시간복잡도가 O(log n) 입니다",
        "시청자 여러분께 감사드린다는 내용이 과제에 나옵니다",  # 상투어를 '포함' 하지만 다른 문장
        "thank you for watching the demo I just showed",
    ],
)
def test_real_speech_is_kept(text):
    """부분 일치로 지우면 멀쩡한 강의 내용이 날아갑니다. 정확히 일치할 때만 지웁니다."""
    drop, _ = Cleaner().should_drop(text, 0.1)
    assert drop is False, text


def test_extra_phrases_from_config():
    cleaner = Cleaner(extra_phrases=("자 그러면",))
    assert cleaner.should_drop("자 그러면", 0.1)[0] is True
    assert cleaner.should_drop("자 그러면 다음으로", 0.1)[0] is False


def test_phrase_filter_can_be_disabled():
    cleaner = Cleaner(drop_phrases=False)
    assert cleaner.should_drop("시청해주셔서 감사합니다", 0.1)[0] is False


# ---------------------------------------------------------------------------
# 무음
# ---------------------------------------------------------------------------
def test_high_no_speech_prob_is_dropped():
    drop, reason = Cleaner().should_drop("어...", 0.95)
    assert drop is True
    assert "무음" in reason


def test_low_no_speech_prob_is_kept():
    assert Cleaner().should_drop("어...", 0.2)[0] is False


def test_threshold_is_configurable():
    strict = Cleaner(no_speech_threshold=0.3)
    assert strict.should_drop("음", 0.5)[0] is True
    lenient = Cleaner(no_speech_threshold=0.99)
    assert lenient.should_drop("음", 0.5)[0] is False


def test_threshold_zero_disables_check():
    assert Cleaner(no_speech_threshold=0).should_drop("음", 0.999)[0] is False


def test_missing_no_speech_prob_is_tolerated():
    """엔진이 이 값을 주지 않을 수도 있습니다."""
    assert Cleaner().should_drop("정상적인 문장입니다", None)[0] is False


# ---------------------------------------------------------------------------
# 반복 루프
# ---------------------------------------------------------------------------
def test_repeat_loop_is_cut():
    """Whisper 가 같은 문장을 수십 번 되풀이하는 실패 양상."""
    cleaner = Cleaner(max_repeats=3)
    results = [cleaner.should_drop("네 네 네", 0.1)[0] for _ in range(10)]
    assert results[:3] == [False, False, False], "앞 3개는 살려야 합니다"
    assert all(results[3:]), "4번째부터는 잘라야 합니다"
    assert cleaner.stats.dropped_repeat == 7


def test_repeat_counter_resets_on_new_text():
    cleaner = Cleaner(max_repeats=2)
    for _ in range(5):
        cleaner.should_drop("같은 말", 0.1)
    assert cleaner.should_drop("다른 말", 0.1)[0] is False
    assert cleaner.should_drop("같은 말", 0.1)[0] is False, "카운터가 초기화돼야 합니다"


def test_repeat_check_can_be_disabled():
    cleaner = Cleaner(max_repeats=0)
    assert all(not cleaner.should_drop("네", 0.1)[0] for _ in range(20))


def test_punctuation_variants_count_as_repeats():
    """'네.' 와 '네!' 는 같은 반복으로 봐야 합니다."""
    cleaner = Cleaner(max_repeats=2)
    for text in ("네.", "네!", "네~", "네"):
        cleaner.should_drop(text, 0.1)
    assert cleaner.stats.dropped_repeat >= 1


# ---------------------------------------------------------------------------
# 기타
# ---------------------------------------------------------------------------
def test_empty_text_is_dropped():
    assert Cleaner().should_drop("   ", 0.1)[0] is True
    assert Cleaner().should_drop("", None)[0] is True


def test_disabled_cleaner_keeps_everything_except_empty():
    cleaner = Cleaner(enabled=False)
    assert cleaner.should_drop("시청해주셔서 감사합니다", 0.99)[0] is False
    assert cleaner.should_drop("", None)[0] is True


def test_stats_and_report():
    cleaner = Cleaner()
    cleaner.should_drop("정상 문장", 0.1)
    cleaner.should_drop("시청해주셔서 감사합니다", 0.1)
    cleaner.should_drop("어", 0.99)
    assert cleaner.stats.kept == 1
    assert cleaner.stats.dropped == 2
    assert "제거" in cleaner.stats.line()
    cleaner.report("테스트")  # 예외 없이 돌아야 합니다


def test_from_config():
    class Cfg:
        values = {
            "transcribe.cleanup.enabled": True,
            "transcribe.cleanup.drop_hallucinations": True,
            "transcribe.cleanup.no_speech_threshold": 0.5,
            "transcribe.cleanup.max_repeats": 2,
            "transcribe.cleanup.extra_phrases": ["테스트 문구"],
        }

        def get(self, k, d=None):
            return self.values.get(k, d)

    cleaner = from_config(Cfg())
    assert cleaner.no_speech_threshold == 0.5
    assert cleaner.max_repeats == 2
    assert cleaner.should_drop("테스트 문구", 0.1)[0] is True


def test_default_threshold_value():
    assert Cleaner().no_speech_threshold == DEFAULT_NO_SPEECH_THRESHOLD


def test_realistic_lecture_transcript():
    """무음이 섞인 실제 강의 전사 흐름."""
    cleaner = Cleaner()
    stream = [
        ("자 오늘은 이진 탐색 트리를 하겠습니다", 0.05),
        ("시청해주셔서 감사합니다", 0.4),          # 판서 중 무음에서 지어냄
        ("왼쪽 자식은 부모보다 작습니다", 0.08),
        ("음", 0.93),                              # 모델이 무음이라고 봄
        ("네", 0.2), ("네", 0.2), ("네", 0.2), ("네", 0.2), ("네", 0.2),  # 루프
        ("그래서 탐색이 O(log n) 입니다", 0.06),
    ]
    kept = [t for t, p in stream if not cleaner.should_drop(t, p)[0]]
    assert "자 오늘은 이진 탐색 트리를 하겠습니다" in kept
    assert "왼쪽 자식은 부모보다 작습니다" in kept
    assert "그래서 탐색이 O(log n) 입니다" in kept
    assert "시청해주셔서 감사합니다" not in kept
    assert "음" not in kept
    assert kept.count("네") == 3, "반복은 앞 3개만 남아야 합니다"


# ---------------------------------------------------------------------------
# 영어 강의 오탐 방지
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Thank you",
        "Thank you.",
        "Thank you!",
        "Bye",
        "Bye.",
        "Okay",
        "Yes",
        "See you next week",
        "Any questions?",
    ],
)
def test_short_english_utterances_are_kept(text):
    """회귀 방지: 영어 수업에서 교수가 수업을 마치며 "Thank you." 라고
    말합니다. 환각 목록에 짧은 말을 넣으면 전사본에서 수업의 끝이
    사라집니다. 짧은 말은 문구 규칙 대신 확률 신호로 거릅니다.
    """
    drop, reason = Cleaner().should_drop(text, 0.1)
    assert drop is False, f"{text!r} 가 {reason} 으로 삭제됐습니다"


@pytest.mark.parametrize(
    "text",
    ["Thank you for watching", "Thanks for watching", "Please subscribe",
     "Like and subscribe", "See you in the next video"],
)
def test_long_youtube_phrases_still_dropped(text):
    """짧은 말을 살리더라도 확실한 자막 상투어는 여전히 지웁니다."""
    assert Cleaner().should_drop(text, 0.1)[0] is True


def test_short_phrase_dropped_when_signal_is_bad():
    """'Thank you' 도 모델이 무음이라고 보면 버립니다.

    no_speech_prob 0.7 은 "70% 확률로 무음" 이라는 뜻입니다. 교수가 또렷이
    "Thank you" 라고 말했다면 이 값이 낮게 나옵니다. 짧은 구간에 0.7 이
    찍혔다면 환각 쪽이 훨씬 유력합니다(실측 기록: Groq 가 1초 무음에
    " you" 를 0.70 으로 내놨습니다).
    """
    assert Cleaner().should_drop("Thank you", 0.7, -1.5)[0] is True
    assert Cleaner().should_drop("Thank you", 0.7, -0.3)[0] is True
    # 신호가 깨끗하면 그대로 둡니다 — 실제 마무리 인사
    assert Cleaner().should_drop("Thank you", 0.15, -0.3)[0] is False


# ---------------------------------------------------------------------------
# avg_logprob 결합 규칙 (Whisper 자체 기본값)
# ---------------------------------------------------------------------------
LONG = "자 그러면 이진 탐색 트리의 삽입 연산을 보겠습니다"


def test_logprob_rule_needs_both_signals_bad():
    """긴 문장으로 확인합니다 — 짧은 구간 규칙에 먼저 걸리면 이 규칙을
    시험하는 게 아니게 됩니다."""
    cleaner = Cleaner()
    # 무음 의심은 있지만 확신도는 괜찮음 -> 유지
    assert cleaner.should_drop(LONG, 0.7, -0.5)[0] is False
    # 확신도는 낮지만 무음 의심은 없음 -> 유지
    assert cleaner.should_drop(LONG, 0.2, -2.0)[0] is False
    # 둘 다 나쁨 -> 제거
    drop, reason = cleaner.should_drop(LONG, 0.7, -2.0)
    assert drop is True
    assert "저확신" in reason


def test_logprob_rule_can_be_disabled():
    cleaner = Cleaner(use_logprob_rule=False)
    assert cleaner.should_drop(LONG, 0.7, -2.0)[0] is False


def test_logprob_missing_skips_the_rule():
    """엔진이 avg_logprob 를 주지 않으면 그 규칙만 건너뜁니다."""
    assert Cleaner().should_drop(LONG, 0.7, None)[0] is False


def test_single_threshold_still_applies_above_085():
    assert Cleaner().should_drop(LONG, 0.9, None)[0] is True


def test_english_lecture_flow():
    """영어 수업 전사 흐름 — 실제 발화는 남고 환각만 빠져야 합니다."""
    cleaner = Cleaner()
    stream = [
        ("Good morning everyone", 0.05, -0.2),
        ("Today we will cover binary search trees", 0.06, -0.3),
        ("Thank you for watching", 0.4, -0.8),     # 환각
        ("Any questions?", 0.1, -0.4),
        ("", 0.9, -1.8),                            # 무음
        ("Okay, see you next week", 0.08, -0.3),
        ("Thank you", 0.12, -0.35),                 # 실제 마무리 인사
    ]
    kept = [t for t, n, a in stream if not cleaner.should_drop(t, n, a)[0]]
    assert kept == [
        "Good morning everyone",
        "Today we will cover binary search trees",
        "Any questions?",
        "Okay, see you next week",
        "Thank you",
    ], kept


# ---------------------------------------------------------------------------
# 표기형 환각 — 내용이 아니라 '상태 꼬리표'
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    ["[음악]", "[Music]", "(웃음)", "[박수]", "[BLANK_AUDIO]", "[inaudible]",
     "(no speech)", "[silence]", "<no audio>", "♪♪♪", "...", "---", "   . . .   "],
)
def test_placeholder_segments_are_dropped(text):
    """강의실은 프로젝터 팬·판서·키보드 소리가 계속 나서
    Whisper 가 이런 꼬리표를 자주 내놓습니다."""
    drop, reason = Cleaner().should_drop(text, 0.3, -0.5)
    assert drop is True, f"{text!r} 가 남았습니다"
    assert "표기형" in reason


@pytest.mark.parametrize(
    "text",
    [
        "자 오늘은 이진 탐색 트리를 하겠습니다",
        "네", "예", "그렇죠", "맞습니다",
        "O(log n) 입니다",
        "(참고) 교재 3장을 보세요",          # 괄호로 '시작' 하지만 내용이 있음
        "트리의 높이는 log n 입니다",
        "1번 문제",
        "3장 45쪽",
    ],
)
def test_real_korean_speech_survives_placeholder_rule(text):
    """회귀 방지: 참고한 구현은 '라틴 문자가 없으면 버린다' 였습니다.
    그대로 가져왔다면 **한국어 전사가 통째로 삭제**됐을 것입니다.
    """
    drop, reason = Cleaner().should_drop(text, 0.15, -0.3)
    assert drop is False, f"{text!r} 가 {reason} 으로 삭제됐습니다"


def test_placeholder_rule_can_be_disabled():
    assert Cleaner(drop_placeholders=False).should_drop("[음악]", 0.2, -0.3)[0] is False


def test_is_placeholder_direct():
    from syncnote.transcribe.cleanup import is_placeholder

    assert is_placeholder("[Music]") is True
    assert is_placeholder("♪") is True
    assert is_placeholder("") is True
    assert is_placeholder("안녕하세요") is False
    assert is_placeholder("Hello") is False
    assert is_placeholder("(참고) 교재를 보세요") is False


# ---------------------------------------------------------------------------
# 짧은 구간 — 실측으로 뚫린 임계값
# ---------------------------------------------------------------------------
def test_measured_groq_silence_hallucination_is_caught():
    """회귀 방지 — 실측 기록된 구멍.

    codyssey_final docs/research/speak-hallucination.md:116 에 따르면
    Groq Whisper 는 1초 무음에 " you" 를 no_speech_prob 0.70,
    avg_logprob -0.71 로 내놨고 "기존 필터 통과" 했습니다.
    우리 필터도 같은 값에서 통과시켰습니다(0.70 < 0.85, -0.71 > -1.0).
    """
    drop, reason = Cleaner().should_drop(" you", 0.70, -0.71)
    assert drop is True, "실측된 무음 환각이 여전히 통과합니다"
    assert "짧은 구간" in reason


def test_measured_openai_silence_hallucination_is_caught():
    """같은 입력에 OpenAI whisper-1 은 0.94 를 줬습니다(:117).
    즉 같은 계열인데 0.70~0.94 로 흔들립니다."""
    assert Cleaner().should_drop("you", 0.94, -0.71)[0] is True


@pytest.mark.parametrize("text", ["네", "예", "Yes", "Okay", "맞아요", "좋습니다"])
def test_short_real_utterances_survive_when_signal_is_clean(text):
    """교수가 한 마디 할 때는 no_speech_prob 가 낮게 나옵니다."""
    assert Cleaner().should_drop(text, 0.15, -0.3)[0] is False


def test_long_segment_is_not_judged_by_short_rule():
    """긴 문장은 증거가 많아 0.85 기준을 그대로 씁니다."""
    long_text = "자 그러면 이진 탐색 트리의 삽입 연산을 보겠습니다"
    assert Cleaner().should_drop(long_text, 0.70, -0.71)[0] is False


def test_short_rule_threshold_is_configurable():
    lenient = Cleaner(short_segment_no_speech=0.99)
    assert lenient.should_drop(" you", 0.70, -0.71)[0] is False
    off = Cleaner(short_segment_no_speech=0)
    assert off.should_drop(" you", 0.70, -0.71)[0] is False


# ---------------------------------------------------------------------------
# 임계값을 나중에 고칠 수 있게 숫자를 남기는가
# ---------------------------------------------------------------------------
def test_dropped_samples_keep_the_numbers():
    """본문만 남기면 임계값이 맞는지 틀린지 판단할 자료가 없습니다."""
    cleaner = Cleaner()
    cleaner.should_drop("시청해주셔서 감사합니다", 0.42, -0.9)
    sample = cleaner.stats.samples[0]
    assert sample["no_speech_prob"] == pytest.approx(0.42)
    assert sample["avg_logprob"] == pytest.approx(-0.9)
    assert sample["reason"] == "상투어"
    assert "시청해주셔서" in sample["text"]


def test_drop_ratio_is_tracked():
    cleaner = Cleaner()
    for i in range(8):
        cleaner.should_drop(f"정상 문장 {i} 입니다", 0.1, -0.3)
    cleaner.should_drop("[음악]", 0.1, -0.3)
    cleaner.should_drop("(웃음)", 0.1, -0.3)
    assert cleaner.stats.kept == 8
    assert cleaner.stats.dropped == 2
    assert cleaner.stats.drop_ratio == pytest.approx(0.2)


def test_excessive_removal_warns(caplog):
    """너무 많이 지웠으면 사용자에게 알려야 합니다.
    교수의 실제 말이 조용히 사라지는 쪽이 환각이 남는 쪽보다 나쁩니다."""
    import logging

    cleaner = Cleaner()
    for i in range(3):
        cleaner.should_drop(f"정상 문장 {i}", 0.1, -0.3)
    for i in range(10):
        cleaner.should_drop(f"[음악 {i}]", 0.1, -0.3)
    with caplog.at_level(logging.WARNING, logger="syncnote.transcribe.cleanup"):
        cleaner.report("테스트")
    assert any("과하게" in r.message for r in caplog.records), caplog.records
