"""웹 뷰어 콘텐츠 스크립트의 자격증명 제외 로직 — 실제 브라우저 DOM 에서 검증.

확장을 패키징/로드하지 않고, content.js 의 순수 로직(window.__syncnoteScan)을
Playwright 로 띄운 Chromium 페이지에 주입해 '담김/제외'를 실측합니다. 이게 보안
핵심(비밀번호가 안 담기는가)이라, jsdom 이 아니라 진짜 브라우저로 확인합니다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright 미설치"
).sync_playwright

CONTENT = Path(__file__).resolve().parents[1] / "extension" / "content.js"
CHROMIUM_CANDIDATES = [os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"]

HTML = """
<!doctype html><meta charset=utf-8><body>
  <input type="text" name="fullname" value="Hong">
  <textarea name="memo">line one</textarea>
  <select name="country"><option selected>KR</option></select>
  <div contenteditable="true" id="notes">note text</div>

  <input type="password" name="loginpw" value="secretpw">
  <input type="text" name="otpcode" autocomplete="one-time-code" value="123456">
  <input type="text" name="applicant_ssn" value="900101">

  <form>
    <input type="password" name="pw2" value="y">
    <input type="text" name="username_in_form" value="u">
  </form>
</body>
"""


@pytest.fixture(scope="module")
def scanned():
    with sync_playwright() as pw:
        last = None
        for path in [None, *CHROMIUM_CANDIDATES]:
            if path is not None and not Path(path).exists():
                continue
            try:
                kw = {"headless": True, "args": ["--no-sandbox"]}
                if path:
                    kw["executable_path"] = path
                br = pw.chromium.launch(**kw)
                break
            except Exception as exc:  # pragma: no cover
                last = exc
        else:
            pytest.skip(f"Chromium 을 띄울 수 없습니다: {last}")
        page = br.new_page()
        page.set_content(HTML)
        page.add_script_tag(path=str(CONTENT))
        result = page.evaluate("window.__syncnoteScan()")
        br.close()
        return result


def _captured(scanned):
    return {f["name"]: f["value"] for f in scanned["captured"]}


def _skipped(scanned):
    return {f["name"]: f["reason"] for f in scanned["skipped"]}


def test_only_the_safe_fields_are_captured(scanned):
    assert set(_captured(scanned)) == {"fullname", "memo", "country", "notes"}


def test_captured_values_are_read(scanned):
    cap = _captured(scanned)
    assert cap["fullname"] == "Hong" and cap["memo"] == "line one"
    assert cap["country"] == "KR" and cap["notes"] == "note text"


def test_password_type_field_is_skipped(scanned):
    assert _skipped(scanned).get("loginpw") == "password_field"


def test_credential_autocomplete_is_skipped(scanned):
    # type=text 지만 autocomplete=one-time-code → 제외.
    assert _skipped(scanned).get("otpcode") == "password_field"


def test_form_containing_a_password_poisons_the_whole_form(scanned):
    sk = _skipped(scanned)
    assert sk.get("pw2") == "password_field"
    assert sk.get("username_in_form") == "form_has_password"


def test_name_blacklist_field_is_skipped(scanned):
    assert _skipped(scanned).get("applicant_ssn") == "name_blacklist"


def test_no_password_value_anywhere_in_captured(scanned):
    blob = repr(scanned["captured"])
    assert "secretpw" not in blob and "123456" not in blob and "900101" not in blob


# -- acrobat.adobe.com 프로필 (실측 DOM 구조로 회귀 고정) ----------------------
# 라이브 acrobat.adobe.com 에서 확인한 구조: PDF 폼 입력은 input[data-testid="textInput"],
# 비번 필드는 type=password. AI 질문칸(QnAQuestionInput)·페이지번호칸은 비-폼이라 제외.
HTML_ADOBE = """
<!doctype html><meta charset=utf-8><body>
  <input data-testid="textInput" type="text" name="FullName" value="Kim">
  <input data-testid="textInput" type="text" name="Address" value="">
  <input data-testid="textInput" type="text" name="Phone" value="">
  <input data-testid="textInput" type="password" name="SecretPin" value="TOPSECRET">
  <textarea data-testid="QnAQuestionInput" placeholder="Ask a question">my private question</textarea>
  <input id="PageNumberUIModern" type="text" value="3" aria-label="Go to page number">
  <input type="text" name="UnrelatedField" value="elsewhere on page">
</body>
"""


@pytest.fixture(scope="module")
def scanned_adobe():
    with sync_playwright() as pw:
        last = None
        for path in [None, *CHROMIUM_CANDIDATES]:
            if path is not None and not Path(path).exists():
                continue
            try:
                kw = {"headless": True, "args": ["--no-sandbox"]}
                if path:
                    kw["executable_path"] = path
                br = pw.chromium.launch(**kw)
                break
            except Exception as exc:  # pragma: no cover
                last = exc
        else:
            pytest.skip(f"Chromium 을 띄울 수 없습니다: {last}")
        page = br.new_page()
        page.set_content(HTML_ADOBE)
        page.add_script_tag(path=str(CONTENT))
        page.evaluate("(h) => { window.__syncnoteProfileHost = h; }", "acrobat.adobe.com")
        result = page.evaluate("window.__syncnoteScan()")
        br.close()
        return result


def test_adobe_captures_only_pdf_form_fields(scanned_adobe):
    # data-testid=textInput 인 폼 필드만. AI 질문칸·페이지번호·무관 입력은 제외.
    assert set(_captured(scanned_adobe)) == {"FullName", "Address", "Phone"}


def test_adobe_password_field_is_skipped(scanned_adobe):
    assert _skipped(scanned_adobe).get("SecretPin") == "password_field"


def test_adobe_non_form_inputs_are_not_touched(scanned_adobe):
    blob = repr(scanned_adobe)
    assert "my private question" not in blob   # AI 질문칸
    assert "UnrelatedField" not in blob         # 폼 밖 입력
    assert "TOPSECRET" not in blob              # 비번 값
