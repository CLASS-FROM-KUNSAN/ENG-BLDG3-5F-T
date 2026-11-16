"""활동 기록 정책 엔진 — 자격증명/민감 필드 제외와 레드액션.

어댑터가 넘긴 '필드 값'을 담을지 버릴지. 키 입력은 다루지 않습니다.
"""

from __future__ import annotations

import pytest

from gymhall.activity.policy import FF_PASSWORD, FF_RICHTEXT, FieldEvent, Policy, redact


def web_policy(**kw) -> Policy:
    base = {"origins": ("https://notes.example.com",), "process_scope": False,
            "name_blacklist": ("*ssn*", "*주민*", "*card*")}
    base.update(kw)
    return Policy(**base)


def ev(**kw) -> FieldEvent:
    base = {"adapter": "pdfjs", "value": "메모", "origin": "https://notes.example.com"}
    base.update(kw)
    return FieldEvent(**base)


# -- 스코프 -------------------------------------------------------------------
def test_web_origin_not_on_allowlist_is_dropped():
    d = web_policy().decide(ev(origin="https://evil.example.com"))
    assert not d.capture and d.reason == "non_allowlisted"


def test_web_with_empty_allowlist_drops_everything():
    d = Policy(origins=()).decide(ev())
    assert not d.capture and d.reason == "non_allowlisted"


def test_desktop_requires_process_scope():
    # origin=None 은 데스크톱. process_scope 없으면 버림, 있으면 통과.
    assert Policy(process_scope=False).decide(ev(adapter="acrobat_js", origin=None)).reason == \
        "non_allowlisted"
    d = Policy(process_scope=True).decide(ev(adapter="acrobat_js", origin=None, value="x"))
    assert d.capture


# -- 비밀번호 / 자격증명 ------------------------------------------------------
def test_password_by_is_password_flag_is_dropped():
    d = web_policy().decide(ev(is_password=True))
    assert not d.capture and d.reason == "password_field"


def test_password_by_ff_bit13_is_dropped():
    # PDF AcroForm /Ff 비밀번호 플래그(bit 13)만 봐도 제외.
    d = web_policy().decide(ev(flags=FF_PASSWORD, value="s3cret"))
    assert not d.capture and d.reason == "password_field"


def test_drop_password_cannot_be_bypassed_even_if_name_clean():
    d = web_policy().decide(ev(is_password=True, field_name="memo"))
    assert d.reason == "password_field"


# -- 서명 / 리치텍스트 --------------------------------------------------------
def test_signature_field_dropped():
    assert web_policy().decide(ev(field_type="Sig")).reason == "signature_field"
    assert web_policy().decide(ev(is_signature=True)).reason == "signature_field"


def test_richtext_dropped_only_when_enabled():
    assert web_policy(exclude_richtext=False).decide(ev(flags=FF_RICHTEXT)).capture
    assert web_policy(exclude_richtext=True).decide(ev(flags=FF_RICHTEXT)).reason == \
        "richtext_field"


# -- 이름 블랙리스트 ----------------------------------------------------------
@pytest.mark.parametrize("name", ["SSN", "applicant_ssn", "주민등록번호", "card_no"])
def test_name_blacklist_drops(name):
    assert web_policy().decide(ev(field_name=name)).reason == "name_blacklist"


def test_name_blacklist_is_suppressor_not_promoter():
    # 블랙리스트에 안 걸려도, 비밀번호 신호가 있으면 여전히 제외.
    d = web_policy().decide(ev(field_name="free_memo", is_password=True))
    assert d.reason == "password_field"


# -- 담기 + 레드액션 ----------------------------------------------------------
def test_clean_field_is_captured():
    d = web_policy().decide(ev(value="이진 탐색 트리 정리"))
    assert d.capture and d.value == "이진 탐색 트리 정리" and d.reason == ""


def test_capture_redacts_rrn_and_luhn_card():
    d = web_policy().decide(ev(value="주민 900101-1234567 카드 4242 4242 4242 4242 끝"))
    assert d.capture
    assert "[주민번호]" in d.value and "[카드번호]" in d.value
    assert "900101" not in d.value and "4242" not in d.value


def test_redact_off_keeps_raw_value():
    d = web_policy(redact_patterns=False).decide(ev(value="900101-1234567"))
    assert d.capture and d.value == "900101-1234567"


def test_redact_leaves_non_luhn_digits_alone():
    # Luhn 실패한 16자리는 카드가 아닐 가능성이 높아 가리지 않습니다.
    assert redact("1234567890123456") == "1234567890123456"


def test_policy_from_settings_maps_fields():
    s = {
        "origins": ["https://a.test"],
        "process_allowlist": ["Acrobat.exe"],
        "policy": {"drop_password": True, "exclude_signature": False,
                   "name_blacklist": ["*pin*"], "redact_patterns": False},
    }
    p = Policy.from_settings(s)
    assert p.origins == ("https://a.test",) and p.process_scope is True
    assert p.exclude_signature is False and p.name_blacklist == ("*pin*",)
    assert p.redact_patterns is False
