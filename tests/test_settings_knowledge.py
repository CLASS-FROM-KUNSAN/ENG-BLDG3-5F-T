"""설정값이 어긋날 때: 원문 · 코드가 아는 설정(근거 줄) · 추측 · 교차검증 부탁."""

from pathlib import Path

import pytest

from syncnote import settings_knowledge as K
from syncnote import settings_schema as S

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text(encoding="utf-8")


def test_code_knowledge_points_at_the_real_lines():
    k = K.knowledge("recording.video.fps")
    assert k.known and k.default == 10
    assert k.default_ref.path.endswith("syncnote/config.py")
    assert '"fps": 10' in k.default_ref.code
    assert any("_positive" in r.code for r in k.checks)
    assert not any(r.code.startswith('"""') for r in k.checks)    # 설명글은 '검사' 가 아님
    assert any("recording.video.fps" in r.code for r in k.uses)


def test_allowed_values_come_from_both_check_styles():
    assert K.knowledge("recording.container").allowed == ["mkv", "mp4"]          # not in {...}
    assert K.knowledge("recording.audio.track_layout").allowed == ["both", "mixed", "separate"]


@pytest.mark.parametrize("value, like, want", [
    ("10", 10, 10), ("열", 10, 10), ("2.5", 1.0, 2.5), ("켬", True, True), ("false", True, False),
    (10, "", "10"), ("a, b", [], ["a", "b"]), (10, 10, K.MISSING), ("빨강", 10, K.MISSING),
])
def test_coerce_guesses_what_was_meant(value, like, want):
    assert K.coerce(value, like) == want


def test_example_and_code_agree_today():
    """지금 예시 파일과 코드는 어긋나지 않습니다(어긋나면 이 테스트가 먼저 알려 줌)."""
    keys = {f.dotted for s in S.parse(EXAMPLE) for f in s.fields}
    bad = {}
    for s in S.parse(EXAMPLE):
        for f in s.fields:
            rep = K.analyze(f.dotted, example_value=f.value, example_keys=keys)
            if rep:
                bad[f.dotted] = rep.guesses
    assert bad == {}


def test_renamed_key_is_guessed_from_the_code():
    keys = {f.dotted for s in S.parse(EXAMPLE) for f in s.fields} - {"recording.video.crf"}
    rep = K.analyze("recording.video.crf_value", example_value=30, example_keys=keys)
    assert rep and "recording.video.crf" in rep.guesses[0]


def test_type_change_in_config_suggests_a_value():
    rep = K.analyze("recording.video.fps", example_value=10, current_value="10")
    assert rep.suggestion == 10
    assert any("숫자" in g for g in rep.guesses)


def test_broken_line_value_is_read_with_the_code_kind():
    b = S.Broken(5, "fps = 열", "값의 모양이 TOML 규칙과 달라요", ["recording.video.fps"], end=5)
    rep = K.analyze("recording.video.fps", broken=[("config.example.toml", b)], shown=10)
    assert rep.suggestion == 10


def test_original_block_keeps_comments_verbatim():
    line = K.locate(EXAMPLE, "recording.video.fps")
    o = K.original("config.example.toml", EXAMPLE, line)
    assert o.lines[-1].startswith("fps")
    assert all(x.startswith("#") for x in o.lines[:-1]) and len(o.lines) > 1
    assert EXAMPLE.split("\n")[o.start - 1] == o.lines[0]


def test_html_has_all_four_parts_and_never_says_cannot_read():
    b = S.Broken(5, "fps = 열", S._friendly("Invalid value (at line 5, column 7)"),
                 ["recording.video.fps"], end=5)
    rep = K.analyze("recording.video.fps", broken=[("config.example.toml", b)], shown=10,
                    originals=[K.Original("config.example.toml", 5, ["# 설명", "fps = 열"])])
    h = K.html(rep)
    for part in ("① 원문", "② 제가 알고 있는 설정", "③ 그래서 이런 것이 바뀐 것 같아요", K.UNSURE,
                 "fps = 열", "config.py:"):
        assert part in h
    assert "읽을 수 없" not in h
