"""웹 뷰어 콘텐츠 스크립트의 자격증명 제외 로직 — 실제 브라우저 DOM 에서 검증.

확장을 패키징/로드하지 않고, content.js 의 순수 로직(window.__gymhallScan)을
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
        result = page.evaluate("window.__gymhallScan()")
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
