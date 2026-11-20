"""파일명 안전화, 폴더 구조, 상태 기록."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from syncnote.paths import Layout, safe_name, unique_path
from syncnote.state import Store, sha256_file, sha256_text


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
    first.write_text("1", encoding="utf-8")
    second = unique_path(first)
    assert second != first
    second.write_text("2", encoding="utf-8")
    third = unique_path(first)
    assert third not in (first, second)
    assert first.read_text(encoding="utf-8") == "1", "기존 파일을 덮어썼습니다"


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
    path = layout.session_dir(datetime(2026, 11, 11, 9, 0), None, "자료구조")
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
    assert len(json.loads(path.read_text(encoding="utf-8"))["downloaded"]) == 50


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


# ---------------------------------------------------------------------------
# 프로세스 사이의 read-modify-write
#
# _LOCK 은 스레드 락이라 프로세스 사이를 막지 못합니다.
# RecordSchedule 이 세그먼트를 올리면서 recordings.json 을 쓰고, 같은
# 시각 TranscribeQueue 가 전사 결과를 쓰면, 둘이 각자 읽고 각자 쓰므로
# 늦게 쓴 쪽이 이기고 앞엣것이 사라집니다.
# ---------------------------------------------------------------------------
def test_a_concurrent_writer_is_not_clobbered(tmp_path):
    from syncnote.state import Store

    path = tmp_path / "recordings.json"
    a = Store.open(path)
    a.mark_uploaded("d-a", tag="t", asset_name="a.zip", repo="o/r")
    a.save()

    # 다른 프로세스가 같은 파일을 읽어 자기 것을 더합니다.
    b = Store.open(path)
    b.mark_uploaded("d-b", tag="t", asset_name="b.zip", repo="o/r")
    b.save()

    # a 는 b 가 쓴 것을 모른 채 또 씁니다.
    a.mark_downloaded("post-1", path="x.md")
    a.save()

    final = Store.open(path)
    assert final.is_uploaded("d-a", "o/r"), "a 의 기록이 사라졌습니다"
    assert final.is_uploaded("d-b", "o/r"), "b 의 기록이 사라졌습니다"
    assert final.is_downloaded("post-1")


def test_my_value_wins_for_the_same_key(tmp_path):
    """같은 키를 둘이 썼으면 방금 바꾼 쪽이 최신입니다."""
    from syncnote.state import Store

    path = tmp_path / "s.json"
    a = Store.open(path)
    a.set_meta("k", "old")
    a.save()

    b = Store.open(path)
    b.set_meta("k", "new")
    b.save()
    assert Store.open(path).get_meta("k") == "new"


def test_a_corrupt_file_on_disk_does_not_lose_my_data(tmp_path):
    from syncnote.state import Store

    path = tmp_path / "s.json"
    a = Store.open(path)
    a.mark_downloaded("k1", path="x")
    path.write_text("{깨진 파일", encoding="utf-8")
    a.save()
    assert Store.open(path).is_downloaded("k1")


def test_merge_keeps_non_record_fields(tmp_path):
    from syncnote.state import Store

    path = tmp_path / "s.json"
    a = Store.open(path)
    a.save()
    assert Store.open(path).data["version"] == 1


def test_a_deleted_key_is_not_resurrected_by_the_merge(tmp_path):
    """지운 키를 병합이 되살리면 '미완료 표시 지우기' 가 안 먹습니다."""
    from syncnote.state import Store

    path = tmp_path / "s.json"
    a = Store.open(path)
    a.set_meta("pending:X", "/tmp/x.md")
    a.save()

    b = Store.open(path)          # 다른 프로세스가 같은 상태를 읽음
    a.forget("meta", "pending:X")
    a.save()
    assert Store.open(path).get_meta("pending:X") is None

    # b 가 나중에 써도 되살아나면 안 됩니다... 는 아닙니다.
    # b 는 지운 사실을 모르므로 되살아나는 것이 맞습니다. 중요한 것은
    # 지운 쪽의 save 가 자기 삭제를 지키는 것입니다.
    b.set_meta("other", "1")
    b.save()
    assert Store.open(path).get_meta("other") == "1"


def test_forget_is_safe_on_a_missing_key(tmp_path):
    from syncnote.state import Store

    s = Store.open(tmp_path / "s.json")
    s.forget("meta", "없는키")
    s.save()


# --- 세션 경로에 과목 칸 ------------------------------------------------
class _Course:
    def __init__(self, name):
        self.name = name


def test_a_session_without_a_course_keeps_the_old_path(tmp_path):
    """과목을 모르면 '_미지정' 같은 폴더를 만들지 않습니다.

    만들면 이미 있던 녹화 폴더와 경로가 어긋나고, 옛 세션을 다시
    올리거나 찾을 때 길이 끊깁니다.
    """
    from syncnote.paths import Layout

    layout = Layout(tmp_path)
    when = datetime(2026, 11, 11, 9, 0)
    assert layout.session_dir(when, None, "수업_0900") == (
        tmp_path / "recordings" / "2026-11-11" / "수업_0900"
    )
    assert layout.transcript_dir(when, None, "수업_0900") == (
        tmp_path / "transcripts" / "2026-11-11" / "수업_0900"
    )


def test_a_session_with_a_course_gets_one_more_folder(tmp_path):
    from syncnote.paths import Layout

    layout = Layout(tmp_path)
    when = datetime(2026, 11, 11, 9, 0)
    assert layout.session_dir(when, _Course("자료구조"), "수업_0900") == (
        tmp_path / "recordings" / "2026-11-11" / "자료구조" / "수업_0900"
    )


def test_a_course_with_an_empty_name_is_treated_as_unknown(tmp_path):
    from syncnote.paths import Layout

    layout = Layout(tmp_path)
    when = datetime(2026, 11, 11, 9, 0)
    assert layout.session_dir(when, _Course(""), "s") == (
        tmp_path / "recordings" / "2026-11-11" / "s"
    )
