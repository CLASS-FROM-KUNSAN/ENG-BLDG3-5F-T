"""true/false 설정의 켬/끔 설명 — 예시 파일 주석에서만 읽음(코드에 적어 두지 않음)."""

from pathlib import Path

import pytest

from syncnote import settings_schema as S

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text(encoding="utf-8")


def _field(text, key="x"):
    return next(f for s in S.parse(text) for f in s.fields if f.key == key)


def test_two_line_form_with_continuation_and_default():
    f = _field("[a]\n# 제목 한 줄\n#   true  = 켬 — 켜면 이렇게 됩니다,\n#           이어지는 설명까지   [기본]\n"
               "#   false = 끔 — 끄면 이렇게 됩니다\n# 그 밖의 설명\nx = true\n")
    tg = S.toggle(f)
    assert tg.on == "켜면 이렇게 됩니다, 이어지는 설명까지" and tg.off == "끄면 이렇게 됩니다"
    assert tg.default is True and tg.complete
    assert S.help_without_toggle(f) == "그 밖의 설명"           # 켬/끔 줄과 제목은 뺌


@pytest.mark.parametrize("line", [
    "# true = 켬 — 무음은 건너뜀 [기본], false = 끔 — 전 구간 전사",
    "# true = 켬 — 무음은 건너뜀 [기본] / false = 끔 — 전 구간 전사",
])
def test_one_line_forms(line):
    tg = S.toggle(_field(f"[a]\n{line}\nx = true\n"))
    assert (tg.on, tg.off, tg.default) == ("무음은 건너뜀", "전 구간 전사", True)


def test_inline_comment_with_indented_continuation():
    text = ("[a]\nx = true   # 메뉴\n           #   true  = 켬 — 메뉴를 수집합니다   [기본]\n"
            "           #   false = 끔 — 메뉴는 수집하지 않습니다\n")
    tg = S.toggle(_field(text))
    assert (tg.on, tg.off, tg.default) == ("메뉴를 수집합니다", "메뉴는 수집하지 않습니다", True)


def test_missing_description_is_reported_not_invented():
    tg = S.toggle(_field("[a]\n# 그냥 스위치\nx = false\n"))
    assert (tg.on, tg.off, tg.default, tg.complete) == ("", "", None, False)


def test_every_switch_in_the_example_explains_on_and_off():
    """예시 파일을 고쳐도 설정 창이 스위치마다 켬/끔을 보여 줄 수 있게 지키는 약속.
    실패하면: 그 키 바로 위에 '#   true  = 켬 — …' / '#   false = 끔 — …' 두 줄을 적고,
    예시 값 쪽 줄 끝에 [기본] 을 붙이세요."""
    bad = []
    for f in (f for s in S.parse(EXAMPLE) for f in s.fields if f.kind == "bool"):
        tg = S.toggle(f)
        if not tg.complete:
            bad.append(f"{f.dotted}: 켬/끔 설명이 없음")
        elif tg.default is not f.value:
            bad.append(f"{f.dotted}: [기본] 표시가 예시 값({f.value})과 다름")
        elif max(len(tg.on), len(tg.off)) > 80:
            bad.append(f"{f.dotted}: 켬/끔 설명이 80자를 넘음 — 자세한 것은 일반 설명 줄로")
    assert not bad, "\n".join(bad)
