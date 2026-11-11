"""전사 결과 파일 포맷."""

from __future__ import annotations

import json

import pytest

from gymhall.transcribe import formats as F

SEGMENTS = [
    F.Segment(0.0, 3.5, "안녕하세요"),
    F.Segment(3.5, 7.25, "오늘은 자료구조입니다"),
    F.Segment(7.25, 9.0, "   "),       # 빈 구간
]


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "00:00:00,000"),
        (3.5, "00:00:03,500"),
        (3725.5, "01:02:05,500"),
        (-1, "00:00:00,000"),          # 음수 방어
        (0.0009, "00:00:00,001"),
    ],
)
def test_timestamp(seconds, expected):
    assert F._timestamp(seconds) == expected


def test_vtt_uses_dot_not_comma():
    """SRT 는 쉼표, WebVTT 는 점입니다. 섞으면 재생기가 무시합니다."""
    assert F._timestamp(3.5, comma=False) == "00:00:03.500"


def test_srt_numbering_skips_blank_segments(tmp_path):
    path = F.write_srt(tmp_path / "a.srt", SEGMENTS)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("1\n")
    assert "2\n" in text
    assert "3\n" not in text, "빈 구간에 번호를 붙였습니다"
    assert "-->" in text


def test_txt_has_header_and_no_timestamps(tmp_path):
    path = F.write_txt(tmp_path / "a.txt", SEGMENTS, header="자료구조 / system")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("자료구조 / system")
    assert "-->" not in text
    assert "안녕하세요" in text


def test_vtt_header(tmp_path):
    text = F.write_vtt(tmp_path / "a.vtt", SEGMENTS).read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")


def test_json_keeps_word_timestamps(tmp_path):
    segs = [F.Segment(0, 1, "안녕", [{"start": 0.0, "end": 0.5, "word": "안녕"}])]
    path = F.write_json(tmp_path / "a.json", segs, meta={"model": "small"})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["meta"]["model"] == "small"
    assert data["segments"][0]["words"][0]["word"] == "안녕"


def test_write_all_respects_requested_formats(tmp_path):
    written = F.write_all(tmp_path / "x", SEGMENTS, ["txt", "srt", "vtt", "json"])
    assert {p.suffix for p in written} == {".txt", ".srt", ".vtt", ".json"}


def test_write_all_ignores_unknown_format(tmp_path):
    written = F.write_all(tmp_path / "x", SEGMENTS, ["txt", "docx"])
    assert [p.suffix for p in written] == [".txt"]


def test_everything_is_utf8(tmp_path):
    """Windows 기본 인코딩(cp949)으로 쓰면 한글이 깨집니다."""
    for fmt in ("txt", "srt", "vtt", "json"):
        path = F.write_all(tmp_path / f"e_{fmt}", SEGMENTS, [fmt])[0]
        raw = path.read_bytes()
        assert "안녕하세요".encode() in raw
