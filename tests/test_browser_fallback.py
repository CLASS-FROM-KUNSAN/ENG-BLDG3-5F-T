"""Chrome Headless Shell 이 없으면 일반 Chromium 으로 창 없이 띄우기.

강의실 PC: `playwright install chromium` 이 학교망 인증서 문제로 일부만 받아
일반 Chromium 은 있는데 headless shell 이 없어 수집이 멈췄습니다.
"""

import types

import pytest

from syncnote.eclass import browser as B

MISSING_SHELL = ("BrowserType.launch: Executable doesn't exist at C:\\Users\\Owner\\AppData\\Local"
                 "\\ms-playwright\\chromium_headless_shell-1248\\chrome-headless-shell-win64\\x.exe")
MISSING_FULL = "BrowserType.launch: Executable doesn't exist at ...\\chromium-1248\\chrome.exe"


def _pw(*outcomes):
    calls = []

    def launch(**kw):
        calls.append(kw)
        out = outcomes[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out
    return types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch)), calls


def test_missing_headless_shell_falls_back_to_full_chromium():
    pw, calls = _pw(RuntimeError(MISSING_SHELL), "browser")
    assert B.launch_chromium(pw, headless=True) == "browser"
    assert calls == [{"headless": True}, {"headless": True, "channel": "chromium"}]


def test_both_missing_says_what_to_install():
    pw, _calls = _pw(RuntimeError(MISSING_SHELL), RuntimeError(MISSING_FULL))
    with pytest.raises(B.BrowserMissing, match="playwright install chromium"):
        B.launch_chromium(pw, headless=True)


def test_other_errors_and_headed_mode_do_not_fall_back():
    pw, calls = _pw(RuntimeError("Target crashed"))
    with pytest.raises(RuntimeError, match="crashed"):
        B.launch_chromium(pw, headless=True)
    assert len(calls) == 1
    pw, calls = _pw(RuntimeError(MISSING_FULL))
    with pytest.raises(RuntimeError):
        B.launch_chromium(pw, headless=False)                 # 창 모드는 headless shell 과 무관
    assert len(calls) == 1


def test_normal_launch_is_untouched():
    pw, calls = _pw("browser")
    assert B.launch_chromium(pw, headless=True) == "browser" and calls == [{"headless": True}]
