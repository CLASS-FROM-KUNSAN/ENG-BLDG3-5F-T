"""깨진 TOML 도 '읽을 수 없습니다' 로 버리지 않고, 깨진 곳만 빼고 읽기."""

from pathlib import Path

import pytest

from syncnote import settings_schema as S

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text(encoding="utf-8")


@pytest.mark.parametrize("text,values,line,why,dropped", [
    ("[a]\nx = 1\n[demo\nrec = true\n[b]\ny = 2\n", {"a.x": 1, "b.y": 2}, 3, "닫는 ']'",
     ["[demo] 섹션 전체"]),
    ('[a]\nx = "abc\ny = 2\n', {"a.y": 2}, 2, "따옴표가 닫히지", ["a.x"]),
    ("[a]\nx 1\ny = 2\n", {"a.y": 2}, 2, "'=' 가 없습니다", ["a.x"]),
    ("[a]\nx = 켬\ny = 2\n", {"a.y": 2}, 2, "true/false", ["a.x"]),
    ("[a]\nx = 1\nx = 2\ny = 3\n", {"a.x": 1, "a.y": 3}, 3, "중복", ["a.x"]),
    ('[a]\nlst = ["a",\n  "b"\ny = 2\nz = 3\n', {"a.y": 2, "a.z": 3}, 2, "닫히지 않았", ["a.lst"]),
])
def test_only_the_broken_part_is_left_out(text, values, line, why, dropped):
    fixed, broken = S.salvage(text)
    assert S.current_values(fixed) == values
    assert len(broken) == 1
    b = broken[0]
    assert (b.line, b.dropped) == (line, dropped) and why in b.why
    assert len(fixed.split("\n")) == len(text.split("\n"))          # 줄 번호 그대로
    assert S.BROKEN_MARK + text.split("\n")[line - 1] in fixed        # 원문은 주석으로 남음


def test_several_broken_places_are_all_reported():
    fixed, broken = S.salvage("[a]\nx = 켬\ny = 2\nz 9\nw = 1\n")
    assert S.current_values(fixed) == {"a.y": 2, "a.w": 1}
    assert [b.line for b in broken] == [2, 4]


def test_good_text_is_untouched():
    assert S.salvage("[a]\nx = 1\n") == ("[a]\nx = 1\n", [])


def test_real_example_with_one_bad_value_keeps_everything_else():
    total = sum(len(s.fields) for s in S.parse(EXAMPLE))
    bad = EXAMPLE.replace("\nfps = 10", "\nfps = 열", 1)
    secs, broken = S.parse_lenient(bad)
    got = {f.dotted for s in secs for f in s.fields}
    assert len(got) == total - 1 and "recording.video.fps" not in got
    assert broken[0].dropped == ["recording.video.fps"]


def test_real_example_with_a_broken_header_keeps_the_other_sections():
    bad = EXAMPLE.replace("\n[recording.video]\n", "\n[recording.video\n", 1)
    secs, broken = S.parse_lenient(bad)
    paths = {s.path for s in secs}
    assert "recording.video" not in paths and {"recording", "recording.audio"} <= paths
    assert broken[0].dropped == ["[recording.video] 섹션 전체"]
