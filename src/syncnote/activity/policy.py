"""활동 기록 정책 엔진 — 어떤 필드 값을 담고 무엇을 버리는가.

키 입력을 가로채지 않습니다. 어댑터(PDF.js·Acrobat)가 **커밋된 폼 필드
값**을 이 엔진에 넘기고, 엔진이 '담기(capture)/버리기(drop)'를 정합니다.

자격증명 제외는 휴리스틱이 아니라 어댑터가 읽어 온 **스펙 신호**로 판정합니다:
AcroForm 의 `/Ff` 비밀번호 플래그(bit 13), 웹의 `type=password`, 데스크톱의
UIA `IsPassword`. 규칙은 우선순위이고, 하나라도 '버리기'면 그걸로 끝입니다.
끝까지 내려온 것만 담깁니다(fail-closed: 애매하면 버림).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

# AcroForm 텍스트 필드 플래그(/Ff). PDF 32000-1 표 228. 비트 위치는 1-기준이라
# 비트 N 의 값은 2^(N-1) 입니다 — 헷갈리기 쉬워 실기에서 한 번 틀렸던 부분.
FF_PASSWORD = 1 << 13   # 비트 14, 값 0x2000 — 비밀번호 필드
FF_RICHTEXT = 1 << 25   # 비트 26, 값 0x2000000 — 리치텍스트


@dataclass(frozen=True)
class FieldEvent:
    """어댑터가 넘기는 한 번의 필드 커밋. 원시 키가 아니라 필드 '값'입니다."""

    adapter: str                  # pdfjs | acrobat_web | acrobat_js | acrobat_uia
    value: str
    field_name: str = ""
    field_type: str = "Tx"        # Tx | Sig | Btn | Ch ...
    is_password: bool = False     # 어댑터가 스펙으로 판정(type=password / IsPassword)
    is_signature: bool = False
    is_richtext: bool = False
    flags: int = 0                # 원시 /Ff (PDF 어댑터가 주면)
    origin: str | None = None     # 웹 어댑터만. 데스크톱은 None(프로세스 스코프)
    doc_hash: str = ""
    commit_reason: str = "change"  # blur | change | save | debounce
    ts_ms: int = 0

    def derived_password(self) -> bool:
        return self.is_password or bool(self.flags & FF_PASSWORD)

    def derived_richtext(self) -> bool:
        return self.is_richtext or bool(self.flags & FF_RICHTEXT)


@dataclass
class Decision:
    capture: bool
    reason: str = ""    # 버린 이유 코드(민감 데이터 없음)
    value: str = ""     # 담을 값(레드액션 적용 후). capture=False 면 빈 문자열


# -- 저장 전 레드액션 ---------------------------------------------------------
# 완벽 보장이 아닙니다. 명백한 패턴만 가립니다. 핵심 방어는 필드 단위 제외이고,
# 이건 '지정 메모 필드에 사용자가 직접 민감정보를 쳐 넣은' 잔여 경우용 그물입니다.
_RRN = re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")        # 한국 주민등록번호
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")          # 카드번호 후보 → Luhn 로 확정


def _luhn(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    # 오른쪽에서 두 번째 자리부터 두 배 = 왼쪽 0-기준 인덱스로는 길이와 같은 홀짝.
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str) -> str:
    text = _RRN.sub("[주민번호]", text)
    return _CARD.sub(lambda m: "[카드번호]" if _luhn(m.group()) else m.group(), text)


@dataclass
class Policy:
    origins: tuple[str, ...] = ()       # 웹 어댑터 허용 오리진. 비면 웹은 전부 드롭
    process_scope: bool = False         # 데스크톱(origin=None) 캡처 허용 여부
    drop_password: bool = True          # 고정(설정 검증에서 True 강제)
    exclude_signature: bool = True
    exclude_richtext: bool = False
    name_blacklist: tuple[str, ...] = ()
    redact_patterns: bool = True

    @classmethod
    def from_settings(cls, s: dict) -> Policy:
        p = dict(s.get("policy", {}) or {})
        return cls(
            origins=tuple(s.get("origins", []) or ()),
            process_scope=bool(s.get("process_allowlist")),
            drop_password=bool(p.get("drop_password", True)),
            exclude_signature=bool(p.get("exclude_signature", True)),
            exclude_richtext=bool(p.get("exclude_richtext", False)),
            name_blacklist=tuple(p.get("name_blacklist", []) or ()),
            redact_patterns=bool(p.get("redact_patterns", True)),
        )

    def decide(self, ev: FieldEvent) -> Decision:
        # 1. 스코프 — 허용목록 밖이면 끝(애매하면 버림)
        if ev.origin is not None:
            if not self.origins or ev.origin not in self.origins:
                return Decision(False, "non_allowlisted")
        elif not self.process_scope:
            return Decision(False, "non_allowlisted")

        # 2. 비밀번호 — 스펙 신호로 제외(고정)
        if self.drop_password and ev.derived_password():
            return Decision(False, "password_field")

        # 3. 서명 필드
        if self.exclude_signature and (ev.is_signature or ev.field_type == "Sig"):
            return Decision(False, "signature_field")

        # 4. 리치텍스트
        if self.exclude_richtext and ev.derived_richtext():
            return Decision(False, "richtext_field")

        # 5. 이름 블랙리스트(*ssn*, *주민*, *card* ...) — 억제 전용
        name = (ev.field_name or "").lower()
        if any(fnmatch.fnmatchcase(name, pat.lower()) for pat in self.name_blacklist):
            return Decision(False, "name_blacklist")

        # 6. 담기 — 저장 전 레드액션
        value = redact(ev.value) if self.redact_patterns else ev.value
        return Decision(True, "", value)
