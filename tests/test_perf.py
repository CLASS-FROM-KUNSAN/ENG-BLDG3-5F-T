"""전사 속도 자가보정.

"small 모델이면 1시간에 약 10분" 같은 숫자는 지어낸 것이었습니다.
실제 소요 시간은 기기마다, 같은 기기에서도 CPU 경합에 따라 몇 배씩
차이 납니다. 추정 대신 측정하고, 추정인지 실측인지 밝힙니다.
"""

from __future__ import annotations

import json

import pytest

from gymhall.transcribe.perf import (
    EMA_ALPHA,
    GPU_SPEEDUP_GUESS,
    SEED_RTF_CPU,
    Profile,
    describe_estimate,
    human_duration,
    seed_rtf,
)


@pytest.fixture
def profile(tmp_path):
    return Profile(tmp_path / "perf.json")


# ---------------------------------------------------------------------------
def test_seed_before_any_measurement(profile):
    """실측이 없으면 대략값을 씁니다."""
    assert profile.rtf("small", "cpu") == SEED_RTF_CPU["small"]
    assert profile.is_measured("small", "cpu") is False


def test_smaller_model_is_seeded_faster():
    assert seed_rtf("tiny", "cpu") < seed_rtf("small", "cpu") < seed_rtf("medium", "cpu")


def test_gpu_seed_is_much_faster():
    assert seed_rtf("small", "cuda") == pytest.approx(
        SEED_RTF_CPU["small"] * GPU_SPEEDUP_GUESS
    )


def test_unknown_model_gets_a_default():
    assert profile_rtf_for_unknown() > 0


def profile_rtf_for_unknown():
    return seed_rtf("some-future-model", "cpu")


def test_first_measurement_replaces_the_seed(profile):
    profile.record(3600, 1800, "small", "cpu")   # 1시간 오디오를 30분에
    assert profile.rtf("small", "cpu") == pytest.approx(0.5)
    assert profile.is_measured("small", "cpu") is True


def test_second_measurement_is_smoothed(profile):
    profile.record(3600, 1800, "small", "cpu")   # 0.5
    profile.record(3600, 3600, "small", "cpu")   # 1.0
    expected = EMA_ALPHA * 1.0 + (1 - EMA_ALPHA) * 0.5
    assert profile.rtf("small", "cpu") == pytest.approx(expected)


def test_cpu_and_gpu_are_tracked_separately(profile):
    """같은 통계에 섞으면 예상이 크게 틀어집니다."""
    profile.record(3600, 3600, "small", "cpu")
    profile.record(3600, 120, "small", "cuda")
    assert profile.rtf("small", "cpu") > profile.rtf("small", "cuda") * 10


def test_models_are_tracked_separately(profile):
    profile.record(3600, 300, "tiny", "cpu")
    profile.record(3600, 3600, "medium", "cpu")
    assert profile.rtf("tiny", "cpu") < profile.rtf("medium", "cpu")


@pytest.mark.parametrize(
    "audio,elapsed",
    [(0, 100), (-5, 100), (100, 0), (100, -5)],
)
def test_nonsense_input_is_ignored(profile, audio, elapsed):
    assert profile.record(audio, elapsed, "small", "cpu") is None
    assert profile.is_measured("small", "cpu") is False


def test_absurd_rtf_is_ignored(profile):
    """중간에 멈춘 전사나 시계 변경이 통계를 망치면 안 됩니다."""
    profile.record(3600, 1800, "small", "cpu")
    good = profile.rtf("small", "cpu")
    profile.record(1, 100000, "small", "cpu")    # RTF 100000
    assert profile.rtf("small", "cpu") == pytest.approx(good)


def test_persists_across_instances(tmp_path):
    first = Profile(tmp_path / "perf.json")
    first.record(3600, 1800, "small", "cpu")
    second = Profile(tmp_path / "perf.json")
    assert second.rtf("small", "cpu") == pytest.approx(0.5)
    assert second.get("small", "cpu").samples == 1


def test_corrupt_file_is_recovered(tmp_path):
    path = tmp_path / "perf.json"
    path.write_text("{{{ not json", encoding="utf-8")
    profile = Profile(path)
    assert profile.is_measured("small", "cpu") is False
    profile.record(3600, 1800, "small", "cpu")
    assert Profile(path).is_measured("small", "cpu") is True


def test_wrong_shape_is_tolerated(tmp_path):
    path = tmp_path / "perf.json"
    path.write_text('["unexpected"]', encoding="utf-8")
    assert Profile(path).is_measured("small", "cpu") is False


def test_unwritable_path_does_not_raise(tmp_path):
    """예상 시간 표시가 안 될 뿐, 전사는 계속돼야 합니다."""
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    profile = Profile(blocked / "nested" / "perf.json")
    profile.record(3600, 1800, "small", "cpu")   # 예외 없이 넘어가야 합니다


def test_sample_count_increases(profile):
    for _ in range(3):
        profile.record(3600, 1800, "small", "cpu")
    assert profile.get("small", "cpu").samples == 3


def test_last_rtf_is_the_raw_observation(profile):
    profile.record(3600, 1800, "small", "cpu")   # 0.5
    profile.record(3600, 3600, "small", "cpu")   # 1.0 관찰
    entry = profile.get("small", "cpu")
    assert entry.last_rtf == pytest.approx(1.0)
    assert entry.rtf != pytest.approx(1.0)       # 평균은 다름


def test_json_is_readable(tmp_path):
    profile = Profile(tmp_path / "perf.json")
    profile.record(3600, 1800, "small", "cpu")
    data = json.loads((tmp_path / "perf.json").read_text(encoding="utf-8"))
    assert "cpu:small" in data


# ---------------------------------------------------------------------------
def test_estimate_scales_with_audio_length(profile):
    profile.record(3600, 1800, "small", "cpu")   # 0.5x
    assert profile.estimate_seconds(7200, "small", "cpu") == pytest.approx(3600)


def test_estimate_has_a_floor(profile):
    assert profile.estimate_seconds(0.1, "tiny", "cpu") >= 1.0


@pytest.mark.parametrize(
    "seconds,expected",
    [(45, "45초"), (90, "2분"), (300, "5분"), (3600, "1시간"),
     (9000, "2시간 30분"), (0, "0초"), (-5, "0초")],
)
def test_human_duration(seconds, expected):
    assert human_duration(seconds) == expected


def test_describe_says_whether_it_is_measured(profile):
    """추정인지 실측인지 밝히지 않으면 사용자가 믿어도 되는지 모릅니다."""
    before = describe_estimate(profile, 3600, "small", "cpu")
    assert "실측 전" in before

    profile.record(3600, 1800, "small", "cpu")
    after = describe_estimate(profile, 3600, "small", "cpu")
    assert "실측 1회" in after
    assert "30분" in after


def test_describe_includes_model_and_device(profile):
    text = describe_estimate(profile, 3600, "medium", "cuda")
    assert "medium" in text and "cuda" in text
