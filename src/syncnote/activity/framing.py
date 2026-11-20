"""네이티브 메시징 프레이밍 — Chrome/Edge 규격.

**중요:** 네이티브 메시징은 줄 단위가 아니라 **4바이트 리틀엔디안 길이 접두사 +
UTF-8 JSON** 바이너리 프로토콜입니다. `for line in sys.stdin` 으로 읽으면 첫
메시지부터 깨집니다. 브라우저→호스트는 메시지당 최대 ~4GB 지만, 폼 필드 값은
작으므로 들어오는 프레임을 1MB 로 제한해 악용·오류를 막습니다.
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAX_IN = 1 * 1024 * 1024   # 들어오는 프레임 상한(1MB)


class FramingError(Exception):
    pass


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """한 프레임을 읽어 dict 로. 스트림이 끝나면 None."""
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        raise FramingError("길이 헤더가 잘립니다")
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return {}
    if length > MAX_IN:
        raise FramingError(f"프레임이 너무 큽니다({length} > {MAX_IN})")
    data = stream.read(length)
    if len(data) < length:
        raise FramingError("본문이 헤더 길이보다 짧습니다")
    return json.loads(data.decode("utf-8"))


def write_message(stream: BinaryIO, obj: Any) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(data)))
    stream.write(data)
    stream.flush()
