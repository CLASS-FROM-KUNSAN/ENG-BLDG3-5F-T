"""config.example.toml → 설정 목록 구조화 · 바꾼 값만 넣어 새 config.toml 만들기."""

import sys
from pathlib import Path

import pytest

from syncnote import settings_schema as S

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = (ROOT / "config.example.toml").read_text(encoding="utf-8")


def _leaves(d, prefix=""):
    """표 배열([[...]])·표 안의 표 목록은 빼고 값 하나짜리 키만."""
    out = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= _leaves(v, dotted)
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            continue
        else:
            out.add(dotted)
    return out


def test_every_setting_in_the_real_example_is_listed():
    """하드코딩이 아니라 예시 파일에서 읽으므로, 예시의 모든 설정이 빠짐없이 나와야 함."""
    fields = {f.dotted: f for s in S.parse(EXAMPLE) for f in s.fields}
    want = _leaves(tomllib.loads(EXAMPLE))
    assert set(fields) == want, (sorted(want - set(fields)), sorted(set(fields) - want))
    data = tomllib.loads(EXAMPLE)
    for f in fields.values():                      # 값도 TOML 이 읽은 그대로
        assert f.value == S._get(data, f.dotted)


def test_real_example_is_grouped_in_a_friendly_order_with_titles_from_comments():
    cats = S.categories(S.parse(EXAMPLE))
    tops = [c.top for c in cats]
    assert tops[:3] == ["general", "eclass", "github"]
    assert all(c.title and not c.title[0].isdigit() for c in cats)   # '1.', '2-1.' 번호 제거
    by = {c.top: c for c in cats}
    assert by["general"].title == "공통 설정"               # '# [general] 공통 설정' 에서
    assert [s.path for s in by["eclass"].sections][0] == "eclass"


def test_real_example_docs_groups_and_shared_comments():
    f = {f.dotted: f for s in S.parse(EXAMPLE) for f in s.fields}
    cur = f["eclass.menus.curriculum"]
    assert cur.group == "학습 목차"                         # '# --- 학습 목차 ---'
    assert "좌우로" in cur.doc                             # 키 아래 들여쓴 이어지는 주석
    assert f["github.repo"].doc == f["github.owner"].doc    # 붙어 쓴 키는 위 설명 공유
    # 설명 없는 스위치에도 이름 — 예시 파일은 이제 모든 키에 설명이 있어서 작은 글로 봅니다.
    bare = [x for s in S.parse("# [demo] 데모 기능\n[demo]\nenabled = true\n") for x in s.fields]
    assert bare[0].title == "데모 기능 켜기"
    assert all(x.doc.strip() for x in f.values())           # 예시 파일은 모든 키에 설명
    assert f["backup.home.exclude_names"].kind == "list"    # 여러 줄 목록


SMALL = """# =====
#  1. 첫 묶음
# =====
[alpha]
# 이름. 첫 줄이 제목
#   둘째 줄은 설명
name = ""

count = 3   # 줄 끝 설명

# --- 소분류 ---
ratio = 1.5
flags = [
    "a",  # 안의 주석
    "b",
]

# [[alpha.items]]
# x = 1

[[alpha.items]]
x = 2

[beta]
enabled = true
"""


def test_parse_small_example():
    secs = S.parse(SMALL)
    assert [s.path for s in secs] == ["alpha", "beta"]
    assert secs[0].title == "첫 묶음"
    f = {f.key: f for f in secs[0].fields}
    assert list(f) == ["name", "count", "ratio", "flags"]       # [[표 배열]] 의 x 는 제외
    assert f["name"].title == "이름. 첫 줄이 제목" and "둘째 줄" in f["name"].doc
    assert f["count"].doc == "줄 끝 설명"
    assert f["ratio"].group == "소분류" and f["flags"].value == ["a", "b"]
    assert secs[1].fields[0].title == "beta 켜기"


def test_keys_written_together_share_the_comment_above():
    """예시 파일 관례: 빈 줄 없이 붙어 쓴 키는 위 주석 하나를 같이 씀
    (owner/repo, draft/prerelease, retry_count/retry_backoff)."""
    secs = S.parse("[a]\n# 재시도\nretry_count = 4\nretry_backoff = 2.0  # 초\n\n# 다른 것\nz = 1\n")
    f = {f.key: f for f in secs[0].fields}
    assert f["retry_count"].title == f["retry_backoff"].title == "재시도"
    assert "초" in f["retry_backoff"].doc and f["z"].title == "다른 것"


def test_apply_changes_only_those_lines_and_keeps_comments():
    new = S.apply(SMALL, {"alpha.name": '따옴표 " 와 \\ 백슬래시', "alpha.count": 7,
                          "alpha.flags": ["c"], "beta.enabled": False})
    assert "# 이름. 첫 줄이 제목" in new and "count = 7   # 줄 끝 설명" not in new
    assert "count = 7  # 줄 끝 설명" in new                   # 줄 끝 주석 유지
    assert '"a",  # 안의 주석' not in new and 'flags = ["c"]' in new
    data = tomllib.loads(new)
    assert data["alpha"]["name"] == '따옴표 " 와 \\ 백슬래시'
    assert data["alpha"]["items"] == [{"x": 2}]                 # 표 배열은 그대로
    assert data["beta"]["enabled"] is False and data["alpha"]["ratio"] == 1.5
    assert S.apply(SMALL, {}) == SMALL                          # 안 바꾸면 그대로


def test_apply_refuses_unknown_keys():
    with pytest.raises(S.SchemaError, match="없는 항목"):
        S.apply(SMALL, {"alpha.nope": 1})


def test_render_values_round_trip():
    for v in (True, 0, -3, 2.0, 1e-3, "", "한글\t탭", [1, 2], ["x", "y"], {"a": 1}):
        assert tomllib.loads(f"v = {S.render(v)}")["v"] == v


def test_problems_use_the_real_validator_and_point_at_fields():
    fields = [f for s in S.parse(EXAMPLE) for f in s.fields]
    base = S.problems(EXAMPLE)
    filled = S.problems(S.apply(EXAMPLE, {"eclass.username": "20261234"}))
    assert len(filled) < len(base) or not base
    bad = S.problems(S.apply(EXAMPLE, {"eclass.username": "x",
                                       "recording.video.backend": "nope"}))
    assert any("recording.video.backend" in p for p in bad)
    hit = [f.dotted for p in bad for f in S.fields_in(p, fields)]
    assert "recording.video.backend" in hit and "recording.audio.backend" not in hit


EXISTING = """# 내 설정
[alpha]
name = "내 이름"   # 손으로 단 주석

# >>> syncnote 시간표 편집기 (이 아래부터 END 까지는 편집기가 다시 씁니다) >>>
[[recording.timetable]]
name = "자료구조"
day = "mon"
start = "09:00"
end = "10:00"
# <<< syncnote 시간표 편집기 <<<
"""


def test_apply_to_existing_config_inserts_missing_keys_and_sections():
    known = {"alpha.name", "alpha.count", "beta.enabled"}
    new = S.apply(EXISTING, {"alpha.name": "바뀜", "alpha.count": 5, "beta.enabled": False},
                  known=known)
    assert '# 내 설정' in new and '# 손으로 단 주석' in new
    lines = new.splitlines()
    assert lines.index("count = 5") == lines.index('name = "바뀜"  # 손으로 단 주석') + 1
    assert lines.index("[beta]") < lines.index(next(ln for ln in lines if ln.startswith("# >>>")))
    data = tomllib.loads(new)
    assert data["alpha"] == {"name": "바뀜", "count": 5} and data["beta"] == {"enabled": False}
    assert data["recording"]["timetable"][0]["name"] == "자료구조"      # 시간표 그대로
    assert S.apply(EXISTING, {}, known=known) == EXISTING


def test_current_values_reads_leaf_keys():
    vals = S.current_values(EXISTING)
    assert vals["alpha.name"] == "내 이름" and "recording.timetable" in vals
    with pytest.raises(S.SchemaError):
        S.current_values("[깨짐")
