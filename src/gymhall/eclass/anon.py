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
# 파일명용: `_` 가 단어 문자라 \b 경계가 안 서므로, 숫자만 양옆에 없도록 봅니다.
_RE_ID_FILE = re.compile(r"(?<!\d)\d{7,10}(?!\d)")

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


def mask_name(value: str, own_names: tuple[str, ...] = ()) -> str:
    """'작성자/제출자' 처럼 구조상 사람 이름인 칸 전체를 가립니다.

    `이름(사번)` 꼴이면 사번까지 함께 `<NAME>(<ID>)` 로, 그 외에 글자가
    남는 '맨 이름'(사번 없는 이름)은 통째로 `<NAME>` 으로 바꿉니다 — 열
    전체가 사람 이름이라고 아는 자리에서만 씁니다. 자유 본문에는 쓰지
    않습니다(거기선 맨 이름을 함부로 못 가립니다). 비면 빈 문자열.
    """
    v = (value or "").strip()
    if not v:
        return ""
    masked = mask_pii(v, own_names)
    # mask_pii 가 아무것도 못 바꿨는데 글자가 남아 있으면 '맨 이름' 입니다.
    if masked == v and re.search(r"[가-힣A-Za-z]", v):
        return "<NAME>"
    return masked


# 파일명용 플레이스홀더는 대괄호를 씁니다. 파일 시스템에서 < > 는
# 금지(특히 Windows)라 각괄호를 쓰면 파일을 못 만듭니다.
def mask_filename(name: str, own_names: tuple[str, ...] = ()) -> str:
    """첨부 파일명 속 확실한 PII 를 가립니다. 확장자는 보존합니다.

    `20201234_홍길동.zip` 같은 제출물 이름에 담긴 본인 학번·이름이 그대로
    디스크에 남지 않게 합니다. 내용(해시)은 건드리지 않으므로 중복 판정에
    영향이 없습니다. 본문과 달리 파일명은 렌더되지 않으니 각괄호 대신 파일
    시스템 안전 문자(대괄호)를 씁니다. 본문 자유텍스트처럼 사번 없는 제3자
    '맨 이름' 은 자동 검출하지 못합니다.
    """
    if not name:
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    base = _RE_NAMEID.sub("[이름]([ID])", base)
    base = _RE_EMAIL.sub("[이메일]", base)
    base = _RE_PHONE.sub("[전화]", base)
    # 파일명은 `20201234_홍길동` 처럼 학번이 밑줄에 붙어 있습니다. `_` 는
    # 단어 문자라 \b 가 서지 않으므로(그래서 _RE_ID 는 여기선 못 씀) 숫자만
    # 양옆에 없도록 보는 경계로 7~10자리 숫자열을 가립니다.
    base = _RE_ID_FILE.sub("[ID]", base)
    for nm in own_names:
        nm = (nm or "").strip()
        if len(nm) >= 2:
            base = base.replace(nm, "[이름]")
    return f"{base}.{ext}" if dot else base
