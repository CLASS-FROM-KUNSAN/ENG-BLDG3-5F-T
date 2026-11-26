"""레포 자동 업데이트(updater.py, scripts/update.bat, AutoUpdate 작업).

사용자 요청: `git pull origin main` 을 주기적으로 하는 로직. 실제 git 으로 원격(bare)·복제본을 만들어
시험합니다(네트워크 없음).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from syncnote import updater as U
from syncnote.recorder import lock as L

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 없습니다")

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def sh(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env).stdout


@pytest.fixture
def repos(tmp_path):
    """origin(bare) · 강의실 PC 의 복제본(pc) · 개발자 쪽 복제본(dev)."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    sh(tmp_path, "clone", str(origin), str(dev))
    (dev / "src").mkdir()
    (dev / "src" / "a.py").write_text("v = 1\n", encoding="utf-8")
    (dev / "requirements.txt").write_text("requests\n", encoding="utf-8")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-m", "first")
    sh(dev, "push", "origin", "HEAD:main")
    pc = tmp_path / "pc"
    sh(tmp_path, "clone", str(origin), str(pc))
    state = tmp_path / "state"
    state.mkdir()
    return origin, dev, pc, state


def push(dev, path, text, msg="change"):
    p = dev / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-m", msg)
    sh(dev, "push", "origin", "HEAD:main")


def run(pc, state, **kw):
    kw.setdefault("pip_runner", lambda repo: pytest.fail("pip 를 부르면 안 됨"))
    kw.setdefault("restarter", lambda repo: ["RecordWatch"])
    return U.update(pc, state_dir=state, **kw)


def test_up_to_date(repos):
    _o, _d, pc, state = repos
    res = run(pc, state)
    assert res.status == "up_to_date"
    assert json.loads((state / "update.json").read_text(encoding="utf-8"))["status"] == "up_to_date"


def test_pulls_new_code_and_restarts_the_recording_programs(repos):
    _o, dev, pc, state = repos
    push(dev, "src/a.py", "v = 2\n")
    res = run(pc, state)
    assert res.status == "updated" and res.changed == ["src/a.py"]
    assert (pc / "src" / "a.py").read_text(encoding="utf-8") == "v = 2\n"
    assert res.restarted == ["RecordWatch"] and res.pip == ""


def test_docs_only_change_does_not_restart(repos):
    _o, dev, pc, state = repos
    push(dev, "docs/x.md", "x\n")
    res = run(pc, state, restarter=lambda repo: pytest.fail("문서만 바뀌었는데 다시 시작"))
    assert res.status == "updated" and res.restarted == []


def test_requirements_change_installs_packages(repos):
    _o, dev, pc, state = repos
    push(dev, "requirements.txt", "requests\nnumpy\n")
    seen = []
    res = run(pc, state, pip_runner=lambda repo: seen.append(repo) or True)
    assert res.pip == "ok" and seen == [pc]


def test_never_while_recording(repos):
    _o, dev, pc, state = repos
    push(dev, "src/a.py", "v = 3\n")
    with L.hold(state / "recording.lock", "자료구조", "schedule"):
        res = run(pc, state)
    assert res.status == "skipped" and "녹화 중" in res.message
    assert (pc / "src" / "a.py").read_text(encoding="utf-8") == "v = 1\n"   # 그대로


def test_conflict_is_rolled_back_and_files_stay(repos):
    _o, dev, pc, state = repos
    push(dev, "src/a.py", "v = 'server'\n")
    (pc / "src" / "a.py").write_text("v = 'local'\n", encoding="utf-8")
    sh(pc, "commit", "-am", "local edit")
    res = run(pc, state)
    assert res.status == "failed" and "되돌림" in res.message
    assert not (pc / ".git" / "MERGE_HEAD").exists()
    assert (pc / "src" / "a.py").read_text(encoding="utf-8") == "v = 'local'\n"
    assert sh(pc, "status", "--porcelain") == ""


def test_local_merge_commits_like_the_classroom_pcs_still_update(repos):
    """강의실 PC 는 병합 커밋이 있는 히스토리(Merge branch 'main' of …) — 빨리 감기가 아니어도 받음."""
    _o, dev, pc, state = repos
    push(dev, "src/b.py", "b = 1\n")
    (pc / "notes.txt").write_text("local\n", encoding="utf-8")
    sh(pc, "add", "notes.txt")
    sh(pc, "commit", "-m", "local only")
    res = run(pc, state)
    assert res.status == "updated" and (pc / "src" / "b.py").exists() and (pc / "notes.txt").exists()


def test_dry_run_changes_nothing(repos):
    _o, dev, pc, state = repos
    push(dev, "src/a.py", "v = 4\n")
    res = run(pc, state, dry_run=True)
    assert res.status == "skipped" and "1개" in res.message
    assert (pc / "src" / "a.py").read_text(encoding="utf-8") == "v = 1\n"


def test_unreachable_remote_fails_without_raising(repos, tmp_path):
    _o, _d, pc, state = repos
    sh(pc, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    res = run(pc, state)
    assert res.status == "failed" and "가져오기 실패" in res.message


def test_not_a_git_checkout(tmp_path):
    assert U.update(tmp_path, state_dir=None).status == "skipped"


# --- 연결 ---------------------------------------------------------------------

def test_update_bat_exits_on_the_same_line():
    """pull 이 update.bat 자신을 바꿔도 cmd 가 엉뚱한 줄을 읽지 않게(한 줄로 끝냄)."""
    bat = (SCRIPTS / "update.bat").read_bytes()
    assert bat.isascii()
    assert b'"%PY%" -m syncnote update %* & exit /b' in bat


def test_registered_as_a_task():
    ps = (SCRIPTS / "register_tasks.ps1").read_text(encoding="utf-8-sig")
    assert 'Register-One "AutoUpdate" (New-HiddenAction "update.bat"' in ps
    assert '"AutoUpdate"' in ps[ps.index("function Remove-All"):ps.index("function New-HiddenAction")]
    from syncnote import tasks
    assert "AutoUpdate" in tasks.EXPECTED


def test_cli(monkeypatch):
    from syncnote import cli

    seen = {}
    monkeypatch.setattr(cli, "_load", lambda a: (_ for _ in ()).throw(RuntimeError("설정 깨짐")))
    monkeypatch.setattr(U, "update", lambda **kw: seen.update(kw) or U.Result("up_to_date", "최신"))
    assert cli.main(["update", "--no-restart"]) == 0
    assert seen["remote"] == "origin" and seen["branch"] == "main" and seen["restart_tasks"] is False
