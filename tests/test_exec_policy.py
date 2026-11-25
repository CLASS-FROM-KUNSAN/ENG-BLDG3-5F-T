"""PowerShell 실행 정책 확인·영구 설정(사용자 요청: 모든 스크립트 실행·모든 자동 등록 전에
Get-ExecutionPolicy 로 확인하고 부족하면 영구적으로 켜는 과정을 정규 과정으로).

실제 정책은 건드리지 않습니다 — 스크립트 내용과 결과 해석, 호출 순서를 봅니다.
(실기 확인은 PR 설명: CurrentUser 를 Restricted 로 바꾼 뒤 .bat 하나로 RemoteSigned 로 돌아옴.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from syncnote import exec_policy as E
from syncnote import tasks as T

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


# --- 결과 해석 -----------------------------------------------------------------

def _json(**kw):
    d = {"before": "RemoteSigned", "before_scope": "CurrentUser", "after": "RemoteSigned",
         "after_scope": "CurrentUser", "changed": [], "gpo_blocked": False, "unblocked": [],
         "error": ""}
    d.update(kw)
    return json.dumps(d)


def test_good_policy_is_ok():
    p = E.parse(_json())
    assert p.ok and p.changed == [] and "RemoteSigned" in p.describe()


def test_restricted_turned_on_permanently():
    # ConvertTo-Json 은 원소 하나짜리 배열을 문자열로 냅니다("CurrentUser")
    p = E.parse(_json(before="Restricted", changed="CurrentUser"))
    assert p.ok and p.changed == ["CurrentUser"]
    assert "Restricted" in p.describe() and "영구" in p.describe()


@pytest.mark.parametrize("policy", ["Restricted", "AllSigned"])
def test_policies_that_block_scripts_are_not_ok(policy):
    p = E.parse(_json(after=policy, after_scope="LocalMachine"))
    assert not p.ok and "실행할 수 없음" in p.describe()


def test_group_policy_is_reported_not_fought():
    p = E.parse(_json(after="AllSigned", after_scope="MachinePolicy", gpo_blocked=True))
    assert not p.ok and p.gpo_blocked and "그룹 정책" in p.describe()


def test_unreadable_output_is_not_checked():
    p = E.parse("오류")
    assert not p.checked and not p.ok


def test_off_switch():
    # conftest 가 SYNCNOTE_NO_TASK_CHECK=1 — 테스트가 실제 정책을 바꾸지 않음
    assert not E.supported()
    assert not E.ensure().checked


def test_powershell_7_module_path_is_not_passed_to_windows_powershell(monkeypatch):
    """PowerShell 7 창에서 시작하면 PSModulePath 가 7 의 모듈을 가리켜 5.1 이
    Get-ExecutionPolicy 를 못 불러왔습니다(이 PC 에서 실제로 걸림)."""
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    assert "PSModulePath" not in E.ps_env() and "PSMODULEPATH" not in E.ps_env()


# --- 자동 등록 전에 -----------------------------------------------------------

def test_every_auto_registration_checks_the_policy_first(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    order = []
    monkeypatch.setattr(E, "ensure", lambda: order.append("policy") or E.Policy(checked=True))
    monkeypatch.setattr(T, "check", lambda repo=None: order.append("check") or T.Status(
        checked=True, present=list(T.EXPECTED)))
    T.ensure(Path(r"C:\repo"))
    assert order[0] == "policy"


# --- 스크립트 -------------------------------------------------------------------

def test_policy_script_uses_remotesigned_for_current_user_and_never_fails():
    ps = (SCRIPTS / "exec_policy.ps1").read_bytes()
    assert ps.startswith(b"\xef\xbb\xbf"), "한글이 있어 BOM 필요(5.1 은 BOM 없으면 cp949)"
    text = ps.decode("utf-8-sig")
    assert "Get-ExecutionPolicy -List" in text
    assert "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser" in text
    assert "Unrestricted -Scope" not in text, "Unrestricted 로 켜지 않음(RemoteSigned 권장)"
    assert "Unblock-File" in text
    # 종료는 늘 0(정책 문제로 스크립트·녹화를 멈추지 않음)
    exits = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("exit")]
    assert exits and all(e == "exit 0" for e in exits)


def test_every_bat_runs_the_policy_step():
    common = (SCRIPTS / "_common.bat").read_text(encoding="ascii")
    assert "exec_policy.ps1" in common and "-ExecutionPolicy Bypass" in common
    assert 'set "PSModulePath="' in common
    helpers = {"_common.bat", "_find_python.bat", "_no_python.bat", "_say.bat"}
    for bat in SCRIPTS.glob("*.bat"):
        if bat.name in helpers:
            continue
        text = bat.read_text(encoding="ascii")
        assert 'call "%~dp0_common.bat"' in text or "exec_policy.ps1" in text, bat.name


def test_activity_host_policy_step_prints_nothing():
    text = (SCRIPTS / "activity_host.bat").read_text(encoding="ascii")
    line = next(ln for ln in text.splitlines() if "exec_policy.ps1" in ln)
    assert "<nul >nul 2>&1" in line, "표준입출력은 브라우저와의 통신용"


def test_setup_and_doctor_show_the_policy():
    for name in ("setup.bat", "doctor.bat"):
        text = (SCRIPTS / name).read_text(encoding="ascii")
        assert text.index('set "SN_POLICY_SHOW=1"') < text.index('call "%~dp0_common.bat"'), name


@pytest.mark.parametrize("name", ["register_tasks.ps1", "register_host.ps1"])
def test_ps1_scripts_run_the_policy_step_first(name):
    text = (SCRIPTS / name).read_text(encoding="utf-8-sig")
    assert 'exec_policy.ps1")' in text
