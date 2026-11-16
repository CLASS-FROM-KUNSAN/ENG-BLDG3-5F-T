"""네이티브 메시징 프레이밍 — 길이 접두사 규격(줄 단위 아님)."""

from __future__ import annotations

import io
import json
import struct

import pytest

from gymhall.activity import framing


def test_write_then_read_roundtrip():
    buf = io.BytesIO()
    framing.write_message(buf, {"v": 1, "value": "안녕"})
    buf.seek(0)
    assert framing.read_message(buf) == {"v": 1, "value": "안녕"}


def test_write_uses_4byte_little_endian_length_prefix():
    buf = io.BytesIO()
    framing.write_message(buf, {"a": 1})
    raw = buf.getvalue()
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert json.loads(raw[4:].decode()) == {"a": 1}


def test_read_returns_none_at_clean_eof():
    assert framing.read_message(io.BytesIO(b"")) is None


def test_read_rejects_truncated_header():
    with pytest.raises(framing.FramingError, match="헤더"):
        framing.read_message(io.BytesIO(b"\x02\x00"))


def test_read_rejects_body_shorter_than_header():
    bad = struct.pack("<I", 100) + b"only-10-by"
    with pytest.raises(framing.FramingError, match="짧"):
        framing.read_message(io.BytesIO(bad))


def test_read_rejects_oversized_frame():
    huge = struct.pack("<I", framing.MAX_IN + 1)
    with pytest.raises(framing.FramingError, match="너무 큽"):
        framing.read_message(io.BytesIO(huge))


def test_two_messages_back_to_back():
    buf = io.BytesIO()
    framing.write_message(buf, {"n": 1})
    framing.write_message(buf, {"n": 2})
    buf.seek(0)
    assert framing.read_message(buf) == {"n": 1}
    assert framing.read_message(buf) == {"n": 2}
    assert framing.read_message(buf) is None
