"""파일명 안전화, 폴더 구조, 상태 기록."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from gymhall.paths import Layout, safe_name, unique_path
from gymhall.state import Store, sha256_file, sha256_text


# ---------------------------------------------------------------------------
# safe_name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("보통 이름.pdf", "보통 이름.pdf"),
        ('a/b\\c:d*?.pdf', "a_b_c_d__.pdf"),
        ("  앞뒤 공백 .hwp", "앞뒤 공백 .hwp"),
        ("", "untitled"),
        ("..", "untitled"),
        ("...", "untitled"),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


@pytest.mark.parametrize("reserved", ["CON", "PRN", "NUL.txt", "COM1", "LPT9.pdf", "aux"])
def test_windows_reserved_names_are_escaped(reserved):
    """Windows 는 이런 이름의 파일을 만들 수 없습니다."""
    out = safe_name(reserved)
    assert out.startswith("_"), out


def test_control_characters_removed():
    assert "\n" not in safe_name("줄\n바꿈\t탭.txt")
    assert "\x00" not in safe_name("널\x00문자.txt")


def test_long_name_keeps_extension():
    out = safe_name("가" * 300 + ".pptx")
    assert len(out) <= 120
    assert out.endswith(".pptx"), "확장자가 사라지면 파일을 열 수 없습니다"


def test_long_name_without_extension():
    out = safe_name("a" * 300)
    assert len(out) <= 120


def test_korean_is_preserved():
    """한글을 음차하거나 지우면 자료를 찾을 수 없게 됩니다."""
    assert "운동과건강" in safe_name("운동과건강 1주차.pptx")


def test_nfc_normalisation():
    """macOS 에서 온 파일명은 NFD 입니다. 같은 글자가 다른 바이트가 됩니다."""
    import unicodedata

    nfd = unicodedata.normalize("NFD", "한글.txt")
    assert safe_name(nfd) == "한글.txt"


# ---------------------------------------------------------------------------
# unique_path
# ---------------------------------------------------------------------------
def test_unique_path_avoids_overwrite(tmp_path):
    first = tmp_path / "a.pdf"
    first.write_text("1")
    second = unique_path(first)
    assert second != first
    second.write_text("2")
    third = unique_path(first)
    assert third not in (first, second)
    assert first.read_text() == "1", "기존 파일을 덮어썼습니다"


def test_unique_path_passthrough(tmp_path):
    target = tmp_path / "new.pdf"
    assert unique_path(target) == target


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def test_layout_structure(tmp_path):
    layout = Layout(tmp_path)
    layout.ensure()
    for d in (layout.eclass, layout.recordings, layout.transcripts,
              layout.logs, layout.state, layout.tmp):
        assert d.is_dir()


def test_layout_sanitises_components(tmp_path):
    layout = Layout(tmp_path)
    path = layout.menu_dir("2026-2학기", "과목/이름:x", "강의자료실")
    assert "/" not in path.name
    assert path.relative_to(tmp_path).parts[0] == "eclass"


def test_session_dir_is_dated(tmp_path):
    layout = Layout(tmp_path)
    path = layout.session_dir(datetime(2026, 11, 11, 9, 0), "자료구조")
    assert "2026-11-11" in str(path)
    assert path.name == "자료구조"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def test_store_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    store = Store.open(path)
    store.mark_downloaded("post:1", path="x.pdf", digest="abc")
    store.mark_uploaded("abc", tag="t", asset_name="a", size=10)
    store.set_meta("last", "2026-11-11")
    store.save()

    again = Store.open(path)
    assert again.is_downloaded("post:1")
    assert again.is_uploaded("abc")
    assert again.get_meta("last") == "2026-11-11"


def test_store_recovers_from_corruption(tmp_path):
    """상태 파일이 깨졌다고 전체가 멈추면 안 됩니다."""
    path = tmp_path / "s.json"
    path.write_text("{{{ 이건 JSON 이 아닙니다", encoding="utf-8")
    store = Store.open(path)
    store.mark_downloaded("post:1")
    store.save()
    assert Store.open(path).is_downloaded("post:1")
    backups = list(tmp_path.glob("s.corrupt-*.json"))
    assert backups, "깨진 파일을 백업하지 않았습니다"


def test_store_handles_wrong_shape(tmp_path):
    """JSON 은 맞지만 구조가 다른 경우."""
    path = tmp_path / "s.json"
    path.write_text('["이건", "배열"]', encoding="utf-8")
    store = Store.open(path)
    assert store.is_downloaded("x") is False
    store.mark_downloaded("x")
    store.save()


def test_store_write_is_atomic(tmp_path):
    """저장 중 전원이 꺼져도 기존 파일이 반쯤 덮이지 않아야 합니다."""
    path = tmp_path / "s.json"
    store = Store.open(path)
    for i in range(50):
        store.mark_downloaded(f"post:{i}")
    store.save()
    # 임시 파일이 남지 않아야 합니다
    assert not list(tmp_path.glob(".tmp-*")), "임시 파일이 남았습니다"
    assert len(json.loads(path.read_text())["downloaded"]) == 50


def test_digest_helpers(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    assert sha256_file(f) == sha256_text("hello")
    g = tmp_path / "b.bin"
    g.write_bytes(b"hello")
    assert sha256_file(f) == sha256_file(g), "같은 내용인데 해시가 다릅니다"


def test_downloaded_digests(tmp_path):
    store = Store.open(tmp_path / "s.json")
    store.mark_downloaded("a", digest="d1")
    store.mark_downloaded("b", digest="d2")
    store.mark_downloaded("c")  # 해시 없음
    assert store.downloaded_digests() == {"d1", "d2"}
