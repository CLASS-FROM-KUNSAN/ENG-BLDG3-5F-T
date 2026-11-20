"""전사 언어 결정.

영어 수업을 "ko" 로 전사하면 소리 나는 대로 한글을 뱉어 그 과목
전사본이 통째로 쓸모없어집니다. 수강 목록에 영어 수업이 하나라도
있으면 전역 설정 하나로는 안 됩니다.
"""

from __future__ import annotations

import pytest

from syncnote.transcribe.language import (
    describe,
    parse,
    restrict,
    whisper_argument,
)


# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("ko", ("ko",)),
        ("en", ("en",)),
        ("ko+en", ("ko", "en")),
        ("en+ko", ("en", "ko")),
        (["ko", "en"], ("ko", "en")),
        ("ko, en", ("ko", "en")),
        ("auto", ()),
        ("", ()),
        (None, ()),
        ("  KO  ", ("ko",)),
        ("영어", ("en",)),
        ("Korean+English", ("ko", "en")),
        ("ko+ko", ("ko",)),          # 중복 제거
        ("ko+auto", ("ko",)),        # auto 는 후보가 아님
    ],
)
def test_parse(value, expected):
    assert parse(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("ko", "ko"), ("en", "en"), ("ko+en", None), ("auto", None), ("", None)],
)
def test_whisper_argument(value, expected):
    """후보가 하나면 고정, 여럿이면 모델에 맡기고 나중에 가둡니다."""
    assert whisper_argument(parse(value)) == expected


def test_describe_is_human_readable():
    assert "고정" in describe(parse("ko"))
    assert "자동" in describe(parse("auto"))
    assert "ko+en" in describe(parse("ko+en"))


# ---------------------------------------------------------------------------
class _Info:
    def __init__(self, language, probability=0.9, all_probs=None):
        self.language = language
        self.language_probability = probability
        self.all_language_probs = all_probs


def test_single_candidate_is_forced():
    """고정했으면 모델이 뭐라 하든 그 언어입니다."""
    got = restrict(_Info("ja", 0.8), parse("en"))
    assert got.code == "en"


def test_detected_inside_candidates_is_kept():
    got = restrict(_Info("en", 0.95), parse("ko+en"))
    assert got.code == "en"
    assert got.restricted is False


def test_wrong_language_is_corrected_to_best_candidate():
    """회귀 방지: 한국어 강의의 영어 용어 구간에서 일본어로 오판하는
    일이 흔합니다. 후보 밖 판정은 후보 안으로 끌어옵니다."""
    info = _Info("ja", 0.55, all_probs=[("ja", 0.55), ("ko", 0.30), ("en", 0.10), ("zh", 0.05)])
    got = restrict(info, parse("ko+en"))
    assert got.code == "ko"
    assert got.restricted is True
    # ko 와 en 만 남겨 재정규화: 0.30 / 0.40 = 0.75
    assert got.probability == pytest.approx(0.75, abs=0.01)


def test_english_wins_when_it_should():
    info = _Info("ja", 0.5, all_probs=[("ja", 0.5), ("ko", 0.1), ("en", 0.4)])
    assert restrict(info, parse("ko+en")).code == "en"


def test_no_candidates_passes_through():
    got = restrict(_Info("ja", 0.8), parse("auto"))
    assert got.code == "ja"
    assert got.restricted is False


def test_missing_probability_table_falls_back_to_first_candidate():
    """엔진이 전체 확률 분포를 주지 않을 수도 있습니다."""
    got = restrict(_Info("ja", 0.8, all_probs=None), parse("ko+en"))
    assert got.code == "ko"
    assert got.restricted is True


def test_empty_probability_table():
    got = restrict(_Info("ja", 0.8, all_probs=[]), parse("ko+en"))
    assert got.code == "ko"


def test_candidates_absent_from_table():
    """후보가 분포에 아예 없으면 첫 후보로 떨어집니다."""
    info = _Info("ja", 0.9, all_probs=[("ja", 0.9), ("zh", 0.1)])
    got = restrict(info, parse("ko+en"))
    assert got.code == "ko"
    assert got.restricted is True


def test_missing_attributes_are_tolerated():
    class Bare:
        pass

    got = restrict(Bare(), parse("auto"))
    assert got.code == ""
