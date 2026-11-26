"""레포 자동 업데이트(`git pull origin main` 을 주기적으로).

강의실 PC 들은 사람이 SSH·원격으로 들어가 `git pull` 을 해야 고친 코드가 들어갔습니다. 그래서
작업 스케줄러의 AutoUpdate 작업(scripts\\update.bat → `syncnote update`)이 로그온 10분 뒤부터
한 시간마다 이것을 합니다.

  1. 녹화 중이면 하지 않음(다음 번에). 녹화 중에 코드·.bat 을 바꾸면 돌고 있는 것과 섞입니다.
  2. `git pull --no-rebase --no-edit <remote> <branch>` — 사람이 하던 그대로(병합 커밋 허용).
     비밀번호·편집기를 묻지 않게(GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never, 시간 제한).
     충돌이 나면 `git merge --abort` 로 되돌리고 기록만 함(파일은 그대로).
  3. requirements.txt 가 바뀌었으면 이 파이썬(.venv)으로 pip install -r.
  4. 코드(src·scripts)가 바뀌었고 녹화 중이 아니면 녹화 프로그램 작업(RecordSchedule·RecordWatch·
     RecordGuard·StreamSchedule)을 다시 시작 — 돌고 있는 파이썬은 옛 코드를 그대로 쥐고 있고,
     cmd 는 실행 중인 .bat 이 바뀌면 엉뚱한 줄을 실행하므로 프로세스 묶음(wscript→cmd→python)을
     통째로 끝내고 작업을 다시 띄웁니다.
결과는 state\\update.json 과 로그에 남깁니다. 무슨 일이 있어도 예외를 밖으로 던지지 않습니다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .logging_setup import get as get_logger

log = get_logger("updater")

RESTART_TASKS = ("RecordSchedule", "RecordWatch", "RecordGuard", "StreamSchedule")
TASK_BATS = {"RecordSchedule": "record_schedule.bat", "RecordWatch": "record_watch.bat",
             "RecordGuard": "record_guard.bat", "StreamSchedule": "stream_schedule.bat"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Result:
    status: str                      # updated · up_to_date · skipped · failed
    message: str = ""
    old: str = ""
    new: str = ""
    changed: list[str] = field(default_factory=list)
    pip: str = ""                    # "" · ok · failed
    restarted: list[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
                "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no", "LC_ALL": "C"})
    return env


def git(repo: Path, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=_git_env(),
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)


def recording_now(state_dir: Path) -> str:
    """녹화 중이면 그 녹화의 라벨, 아니면 ""."""
    from .recorder import lock as rec_lock

    info = rec_lock.read(Path(state_dir) / "recording.lock")
    if info is None or rec_lock._stale(info):
        return ""
    return info.label or "녹화"


def _head(repo: Path) -> str:
    cp = git(repo, "rev-parse", "HEAD")
    return cp.stdout.strip() if cp.returncode == 0 else ""


def update(repo: Path | None = None, *, state_dir: Path | None = None, remote: str = "origin",
           branch: str = "main", install_requirements: bool = True, restart_tasks: bool = True,
           dry_run: bool = False, pip_runner=None, restarter=None) -> Result:
    """한 번 확인하고 필요하면 받습니다. 예외를 던지지 않음."""
    repo = Path(repo or repo_root())
    try:
        res = _update(repo, state_dir, remote, branch, install_requirements, restart_tasks,
                      dry_run, pip_runner or _pip_install, restarter or restart_recording_tasks)
    except Exception as exc:  # noqa: BLE001 — 업데이트 실패로 아무것도 멈추지 않음
        log.exception("자동 업데이트 중 오류")
        res = Result("failed", f"{type(exc).__name__}: {exc}")
    _write_state(state_dir, res)
    return res


def _update(repo, state_dir, remote, branch, install_requirements, restart_tasks, dry_run,
            pip_runner, restarter) -> Result:
    if not (repo / ".git").exists():
        return Result("skipped", "git 레포가 아닙니다(설치본) — 업데이트하지 않음")
    if state_dir is not None:
        busy = recording_now(state_dir)
        if busy:
            return Result("skipped", f"녹화 중이라 다음에 합니다('{busy}')")
    old = _head(repo)
    fetch = git(repo, "fetch", "--quiet", remote, branch)
    if fetch.returncode != 0:
        return Result("failed", "가져오기 실패: " + (fetch.stderr or fetch.stdout).strip()[-300:], old=old)
    behind = git(repo, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD")
    if behind.returncode == 0:
        return Result("up_to_date", "이미 최신입니다", old=old, new=old)
    if dry_run:
        incoming = git(repo, "log", "--oneline", "HEAD..FETCH_HEAD").stdout.strip().splitlines()
        return Result("skipped", f"받을 커밋 {len(incoming)}개(--dry-run 이라 받지 않음)", old=old)
    pull = git(repo, "pull", "--no-rebase", "--no-edit", remote, branch, timeout=300)
    if pull.returncode != 0:
        # 충돌·로컬 변경과 겹침: 하던 병합을 되돌려 폴더를 원래대로
        if (repo / ".git" / "MERGE_HEAD").exists():
            git(repo, "merge", "--abort")
        return Result("failed", "받기 실패(되돌림): " + (pull.stderr or pull.stdout).strip()[-400:],
                      old=old, new=_head(repo))
    new = _head(repo)
    changed = [ln for ln in git(repo, "diff", "--name-only", old, new).stdout.splitlines() if ln]
    res = Result("updated", f"{old[:7]} → {new[:7]} ({len(changed)}개 파일)", old=old, new=new,
                 changed=changed)
    if install_requirements and "requirements.txt" in changed:
        res.pip = "ok" if pip_runner(repo) else "failed"
    code = [f for f in changed if f.startswith(("src/", "scripts/")) or f == "requirements.txt"]
    if restart_tasks and code:
        busy = recording_now(state_dir) if state_dir is not None else ""
        if busy:
            res.message += f" — 녹화 중('{busy}')이라 프로그램은 다음 업데이트 때 다시 시작"
        else:
            res.restarted = restarter(repo)
    return res


def _pip_install(repo: Path) -> bool:
    try:
        cp = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                             "-q", "-r", str(repo / "requirements.txt")], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=1800,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("패키지 설치 실패: %s", exc)
        return False
    if cp.returncode != 0:
        log.error("패키지 설치 실패: %s", (cp.stderr or cp.stdout).strip()[-400:])
    return cp.returncode == 0


def restart_recording_tasks(repo: Path) -> list[str]:
    """녹화 프로그램 작업을 새 코드로 다시 시작(Windows). 다시 띄운 작업 이름들."""
    if sys.platform != "win32":
        return []
    from .exec_policy import _powershell, ps_env

    scripts = str(repo / "scripts").replace("'", "''")
    names = ",".join(f"'{n}'" for n in RESTART_TASKS)
    bats = "|".join(TASK_BATS.values()).replace(".", r"\.")
    # 이 작업들이 띄운 프로세스 묶음(wscript → cmd → python)을 통째로 끝냄: 바뀐 .bat 을 cmd 가
    # 이어 읽지 않게. 그다음 등록돼 있는 작업만 다시 띄움.
    ps = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$s = '" + scripts + "'; "
        "Get-CimInstance Win32_Process -Filter \"Name='wscript.exe' or Name='cmd.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($s) -and $_.CommandLine -match '"
        + bats + "' } | ForEach-Object { taskkill /T /F /PID $_.ProcessId | Out-Null }; "
        "Start-Sleep -Seconds 2; "
        "foreach ($n in @(" + names + ")) { "
        "$t = Get-ScheduledTask -TaskPath '\\Sync Note\\' -TaskName $n -ErrorAction SilentlyContinue; "
        "if ($t -and \"$($t.State)\" -ne 'Disabled') { Start-ScheduledTask -InputObject $t; $n } }"
    )
    try:
        cp = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", ps],
                            capture_output=True, timeout=120, env=ps_env(),
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("녹화 프로그램을 다시 시작하지 못했습니다: %s", exc)
        return []
    return [ln.strip() for ln in cp.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]


def _write_state(state_dir: Path | None, res: Result) -> None:
    if state_dir is None:
        return
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        (Path(state_dir) / "update.json").write_text(
            json.dumps(asdict(res), ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.debug("업데이트 기록 실패: %s", exc)
