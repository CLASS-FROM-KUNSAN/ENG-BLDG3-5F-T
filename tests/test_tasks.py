"""작업 스케줄러 등록 확인·자동 등록(src/syncnote/tasks.py).

실제 강의실 PC 하나는 register_tasks.ps1 을 아무도 실행하지 않아 작업이 하나도 없었고, 그래서
녹화가 한 번도 돌지 않았습니다. 사용자 요청: setup.bat·doctor.bat 에서 감지하고, 모든 스크립트·
프로그램이 실행될 때 자동 등록.

PowerShell 은 가짜로 바꿉니다 — 실제 작업 스케줄러는 건드리지 않습니다.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from syncnote import cli
from syncnote import tasks as T

REPO = Path(r"C:\Program Files\GYM-W-HALL-2F")
SCRIPTS = SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _task(name, repo=REPO, state="Ready"):
    args = f'"{repo}\\scripts\\hidden.vbs" "{repo}\\scripts\\x.bat"'
    return T.Task(name, args, "1121", state)


# --- 판정 -------------------------------------------------------------------

def test_all_expected_tasks_present_is_ok():
    st = T.evaluate([_task(n) for n in T.EXPECTED], REPO)
    assert st.ok and st.missing == [] and len(st.present) == len(T.EXPECTED)


def test_no_tasks_at_all_is_reported_as_missing():
    """강의실 PC 에서 실제로 있었던 상태."""
    st = T.evaluate([], REPO)
    assert not st.ok and st.missing == list(T.EXPECTED)
    assert "RecordSchedule" in st.describe()


def test_tasks_pointing_at_an_old_clone_are_stale():
    """Downloads 에서 Program Files 로 옮기면 작업은 옛 폴더의 스크립트를 부릅니다."""
    old = Path(r"C:\Users\1121\Downloads\GYM-W-HALL-2F")
    st = T.evaluate([_task(n, repo=old) for n in T.EXPECTED], REPO)
    assert not st.ok and st.stale == list(T.EXPECTED)


def test_a_disabled_task_is_reported():
    tasks = [_task(n, state="Disabled" if n == "RecordWatch" else "Ready") for n in T.EXPECTED]
    st = T.evaluate(tasks, REPO)
    assert not st.ok and st.disabled == ["RecordWatch"]


def test_optional_tasks_are_not_required():
    st = T.evaluate([_task(n) for n in T.EXPECTED + T.OPTIONAL], REPO)
    assert st.ok


def test_could_not_read_is_not_ok_but_not_missing():
    st = T.evaluate(None, REPO)
    assert not st.checked and not st.ok and st.missing == []


# --- PowerShell 결과 읽기 ------------------------------------------------------

class _CP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out.encode("utf-8")
        self.stderr = err.encode("utf-8")


def test_query_parses_the_powershell_lines(monkeypatch):
    out = "RecordGuard|\"C:\\r\\scripts\\hidden.vbs\" \"C:\\r\\scripts\\record_guard.bat\"|1121|Ready\n"
    monkeypatch.setattr(T, "_powershell", lambda args, timeout: _CP(0, out))
    got = T.query()
    assert got == [T.Task("RecordGuard", '"C:\\r\\scripts\\hidden.vbs" "C:\\r\\scripts\\record_guard.bat"',
                          "1121", "Ready")]


def test_query_failure_is_none(monkeypatch):
    monkeypatch.setattr(T, "_powershell", lambda args, timeout: _CP(3, "", "모듈 없음"))
    assert T.query() is None


def test_empty_folder_query_is_an_empty_list_not_a_failure():
    """폴더가 없으면 Get-ScheduledTask 가 ObjectNotFound 로 실패합니다 — '작업 없음' 이어야 함
    (아니면 '확인할 수 없음' 으로 보여 등록을 하지 않음). 이 PC 에서 실제로 걸렸습니다."""
    assert "ObjectNotFound" in T._QUERY and "exit 0" in T._QUERY


# --- 등록 --------------------------------------------------------------------

def test_access_denied_retries_with_uac_for_the_original_user(monkeypatch):
    calls = []

    def fake(args, timeout):
        calls.append(args)
        return _CP(T.ACCESS_DENIED if len(calls) == 1 else 0)

    monkeypatch.setattr(T, "_powershell", fake)
    monkeypatch.setattr(T, "_is_admin", lambda: False)
    rc, _ = T.register(REPO, elevate=True, user="1121")
    assert rc == 0 and len(calls) == 2
    assert calls[0][-2:] == ["-User", "1121"]
    assert "RunAs" in calls[1][-1] and '-User "1121"' in calls[1][-1]


def test_background_registration_never_asks_for_uac(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_powershell", lambda a, timeout: calls.append(a) or _CP(T.ACCESS_DENIED))
    monkeypatch.setattr(T, "_is_admin", lambda: False)
    rc, _ = T.register(REPO, elevate=False, user="1121")
    assert rc == T.ACCESS_DENIED and len(calls) == 1


@pytest.fixture
def stamps(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path


def test_ensure_registers_only_when_something_is_missing(stamps, monkeypatch):
    seq = [T.Status(checked=True, missing=["RecordSchedule"]),
           T.Status(checked=True, present=list(T.EXPECTED))]
    monkeypatch.setattr(T, "check", lambda repo=None: seq.pop(0))
    reg = []
    monkeypatch.setattr(T, "register", lambda repo=None, **kw: reg.append(kw) or (0, ""))
    st = T.ensure(REPO, elevate=True)
    assert st.ok and reg == [{"elevate": True}]

    monkeypatch.setattr(T, "check", lambda repo=None: T.Status(checked=True,
                                                               present=list(T.EXPECTED)))
    reg.clear()
    assert T.ensure(REPO).ok and reg == []


def test_ensure_never_raises(stamps, monkeypatch):
    def boom(repo=None):
        raise RuntimeError("작업 스케줄러 서비스가 꺼짐")

    monkeypatch.setattr(T, "check", boom)
    st = T.ensure(REPO)
    assert not st.ok and "꺼짐" in st.error


def test_only_one_registration_at_a_time(stamps):
    assert T._acquire(0)
    try:
        assert not T._acquire(0), "등록이 동시에 두 번 돌면 서로 지우고 만들다 실패합니다"
    finally:
        T._release()
    assert T._acquire(0)
    T._release()


# --- 모든 명령의 시작에서 -----------------------------------------------------

def test_background_check_is_throttled(stamps, monkeypatch):
    monkeypatch.delenv("SYNCNOTE_NO_TASK_CHECK", raising=False)
    monkeypatch.setattr(T, "supported", lambda repo=None: True)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: spawned.append(cmd))
    assert T.spawn_background(REPO)
    assert spawned and spawned[0][-3:] == ["tasks", "ensure", "--auto"]
    assert not T.spawn_background(REPO), "명령마다 PowerShell 을 띄우면 안 됩니다"

    # 고치지 못한 상태면 1시간 뒤, 정상이면 6시간 뒤 다시
    T._write_stamp(REPO, T.Status(checked=True, missing=["RecordWatch"]))
    assert not T.due(REPO) and T.due(REPO, now=time.time() + T.RECHECK_BAD_SEC + 1)
    T._write_stamp(REPO, T.Status(checked=True, present=list(T.EXPECTED)))
    assert not T.due(REPO, now=time.time() + T.RECHECK_BAD_SEC + 1)
    assert T.due(REPO, now=time.time() + T.RECHECK_OK_SEC + 1)


def test_off_switch_and_non_windows_skip(monkeypatch):
    monkeypatch.setenv("SYNCNOTE_NO_TASK_CHECK", "1")
    assert not T.supported()


def test_every_command_starts_the_background_check(monkeypatch):
    seen = []
    monkeypatch.setattr(T, "spawn_background", lambda repo=None: seen.append(1) or True)
    monkeypatch.setattr(cli, "cmd_record_recover", lambda args: 0)
    cli.main(["record", "recover"])
    assert seen == [1]


@pytest.mark.parametrize("argv", [["tasks", "status"], ["doctor"]])
def test_tasks_and_doctor_do_not_spawn_another_check(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(T, "spawn_background", lambda repo=None: seen.append(1) or True)
    monkeypatch.setattr(cli, "cmd_tasks", lambda args: 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    cli.main(argv)
    assert seen == []


# --- 스크립트 ------------------------------------------------------------------

def test_setup_and_doctor_bat_register_missing_tasks():
    setup = (SCRIPTS / "setup.bat").read_text(encoding="ascii")
    doctor = (SCRIPTS / "doctor.bat").read_text(encoding="ascii")
    assert '"%PY%" -m syncnote tasks ensure' in setup
    assert '"%PY%" -m syncnote tasks ensure' in doctor
    # 시간표 편집기는 doctor.bat --json 의 출력을 JSON 으로 읽음 — 그때는 섞지 않음
    assert 'if /i not "%~1"=="--json"' in doctor


def test_register_script_works_without_admin_and_takes_the_user():
    ps = (SCRIPTS / "register_tasks.ps1").read_text(encoding="utf-8-sig")
    assert "Assert-Admin" not in ps, "관리자가 아니면 아예 시도하지 않던 구조"
    assert "[string]$User = $env:USERNAME" in ps
    assert "-UserId $User" in ps and "$env:USERNAME `" not in ps
    assert "exit 5" in ps
