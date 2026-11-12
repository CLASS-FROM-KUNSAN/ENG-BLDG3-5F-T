"""보낸이 익명화.

쪽지 목록의 '보낸이' 칸은 `김진욱 (101394)` 처럼 이름과 사번이 함께
들어 있습니다. 본문은 그대로 두고 이 칸만 바꿉니다.

같은 사람은 실행이 달라도 같은 이름표를 받아야 합니다. 그래야 "이 셋은
같은 사람이 보냈다" 를 알 수 있습니다. 그래서 난수가 아니라 원문의
해시를 씁니다.
"""

from __future__ import annotations

import hashlib
import re

# "이름 (사번)" / "이름(사번)" / "이름 (사번) " 전부 받습니다.
_SENDER = re.compile(r"^\s*(?P<name>.*?)\s*\(\s*(?P<id>[0-9A-Za-z_-]+)\s*\)\s*$")


def token(value: str, *, length: int = 6) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:length]


def sender(value: str) -> str:
    """`김진욱 (101394)` -> `보낸이-3f9a1c`.

    괄호 꼴이 아니면 문자열 전체를 해시합니다. 빈 값은 그대로 둡니다.
    """
    text = (value or "").strip()
    if not text:
        return ""
    m = _SENDER.match(text)
    key = f"{m.group('name')}|{m.group('id')}" if m else text
    return f"보낸이-{token(key)}"
