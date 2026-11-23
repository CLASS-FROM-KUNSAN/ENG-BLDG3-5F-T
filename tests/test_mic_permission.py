"""Windows 마이크 권한: 꺼져 있으면 알리고 켭니다(가짜 레지스트리로)."""

from __future__ import annotations

import pytest

from syncnote.recorder import devices

BASE = "\\".join(devices._CONSENT)
POLICY = "SOFTWARE\\Policies\\Microsoft\\Windows\\AppPrivacy"


class FakeWinreg:
    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values, *, admin=True):
        self.values = dict(values)          # (root, sub, name) -> value
        self.admin = admin
        self.writes = []

    def OpenKey(self, root, sub, _res=0, access=0):  # noqa: N802
        if not any(k[:2] == (root, sub) for k in self.values):
            raise FileNotFoundError(sub)
        if access == self.KEY_SET_VALUE and root == "HKLM" and not self.admin:
            raise PermissionError("관리자 아님")
        reg = self

        class K:
            def __enter__(self):
                return (root, sub)

            def __exit__(self, *a):
                return False
        _ = reg
        return K()

    def QueryValueEx(self, key, name):  # noqa: N802
        try:
            return self.values[(*key, name)], 1
        except KeyError:
            raise FileNotFoundError(name) from None

    def SetValueEx(self, key, name, _r, _t, value):  # noqa: N802
        self.values[(*key, name)] = value
        self.writes.append((*key, value))


def _reg(hklm="Allow", hkcu="Allow", nonpkg="Allow", **kw):
    return FakeWinreg({("HKLM", BASE, "Value"): hklm, ("HKCU", BASE, "Value"): hkcu,
                       ("HKCU", BASE + "\\NonPackaged", "Value"): nonpkg}, **kw)


def test_everything_allowed_changes_nothing():
    w = _reg()
    assert devices.enable_mic_permission(winreg_mod=w) == ([], [])
    assert w.writes == []


def test_user_switches_are_turned_on():
    w = _reg(hkcu="Deny", nonpkg="Deny")
    changed, problems = devices.enable_mic_permission(winreg_mod=w)
    assert len(changed) == 2 and problems == []
    assert w.values[("HKCU", BASE, "Value")] == "Allow"
    assert w.values[("HKCU", BASE + "\\NonPackaged", "Value")] == "Allow"


def test_machine_switch_needs_admin_and_says_so():
    w = _reg(hklm="Deny", nonpkg="Deny", admin=False)
    changed, problems = devices.enable_mic_permission(winreg_mod=w)
    assert changed == ["데스크톱 앱의 마이크 액세스"]
    assert len(problems) == 1 and "관리자 권한" in problems[0]
    assert w.values[("HKLM", BASE, "Value")] == "Deny"


def test_group_policy_is_not_overridden():
    w = _reg(hkcu="Deny")
    w.values[("HKLM", POLICY, "LetAppsAccessMicrophone")] = 2
    changed, problems = devices.enable_mic_permission(winreg_mod=w)
    assert changed == [] and "그룹 정책" in problems[0] and w.writes == []


def test_session_turns_permission_on_and_tries_again(tmp_path, monkeypatch, caplog):
    from syncnote.paths import Layout
    from syncnote.recorder.session import RecordingSession

    class Cfg:
        def get(self, key, default=None):
            return default

    calls = []

    def pick(**_kw):
        calls.append(1)
        if len(calls) == 1:
            return "죽은마이크", [devices.ProbeResult("마이크", 0.0)]
        return "산마이크", [devices.ProbeResult("마이크", 0.02)]
    monkeypatch.setattr(devices, "select_working_mic", pick)
    monkeypatch.setattr(devices, "mic_permission_denied", lambda: True)
    monkeypatch.setattr(devices, "enable_mic_permission", lambda: (["데스크톱 앱의 마이크 액세스"], []))
    s = RecordingSession(Cfg(), Layout(tmp_path), "수업")
    with caplog.at_level("WARNING", logger="syncnote"):
        assert s._resolve_mic() == "산마이크"
    assert len(calls) == 2 and "권한이 꺼져 있어" in caplog.text and "켰습니다" in caplog.text


@pytest.mark.parametrize("denied, rc, text", [(False, 0, "켜져 있습니다"), (None, 0, "확인할 수 없습니다")])
def test_cli_allow_mic_reports(monkeypatch, capsys, denied, rc, text):
    from syncnote import cli

    monkeypatch.setattr(devices, "mic_permission_denied", lambda: denied)
    assert cli._allow_mic() == rc and text in capsys.readouterr().out


def test_cli_allow_mic_turns_on(monkeypatch, capsys):
    from syncnote import cli

    state = {"denied": True}
    monkeypatch.setattr(devices, "mic_permission_denied", lambda: state["denied"])

    def enable():
        state["denied"] = False
        return ["데스크톱 앱의 마이크 액세스"], []
    monkeypatch.setattr(devices, "enable_mic_permission", enable)
    assert cli._allow_mic() == 0
    out = capsys.readouterr().out
    assert "꺼져 있습니다 — 켭니다" in out and "이제 켜져 있습니다" in out
