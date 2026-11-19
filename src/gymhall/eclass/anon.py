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


# --- 개인정보 마스킹 --------------------------------------------------
# 구조가 없는 텍스트에서 확실한 PII 토큰만 가립니다. 내용(제목/날짜/과목명
# 등)은 그대로 둬서 '빠지는 정보' 가 없게 합니다. 이름은 (1) `이름(사번)`
# 꼴, (2) 표의 이름 열, (3) 로그인한 본인 이름 — 이렇게 확실한 자리만
# 가립니다. 본문 자유텍스트 속 제3자 이름까지 자동 검출하지는 않으므로
# 결과물은 로컬 전용입니다(업로드 제외).
_RE_NAMEID = re.compile(r"[가-힣A-Za-z][가-힣A-Za-z ]{0,19}\s*\(\s*\d{4,10}\s*\)")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_PHONE = re.compile(r"\b0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}\b")
_RE_ID = re.compile(r"\b\d{7,10}\b")

# 표에서 이 헤더가 붙은 열은 통째로 <NAME> 으로 가립니다.
NAME_COLUMNS = frozenset({
    "작성자", "작성자명", "글쓴이", "이름", "성명", "성 명",
    "교수", "교수명", "담당교수", "대표교수", "제출자",
})


def mask_pii(text: str, own_names: tuple[str, ...] = ()) -> str:
    """확실한 PII 토큰(이름(사번)·이메일·전화·학번/사번)과 본인 이름을 가립니다.

    순서 중요: `이름(사번)` → 이메일 → 전화 → 학번. 전화를 학번보다 먼저
    가려야 전화의 숫자 조각이 학번으로 잘못 걸리지 않습니다.
    """
    if not text:
        return text
    text = _RE_NAMEID.sub("<NAME>(<ID>)", text)
    text = _RE_EMAIL.sub("<EMAIL>", text)
    text = _RE_PHONE.sub("<PHONE>", text)
    text = _RE_ID.sub("<ID>", text)
    for name in own_names:
        name = (name or "").strip()
        if len(name) >= 2:
            text = text.replace(name, "<NAME>")
    return text
