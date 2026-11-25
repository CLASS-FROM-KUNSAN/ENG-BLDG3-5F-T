"""PowerShell 실행 정책 확인·영구 설정(scripts/exec_policy.ps1 을 부름).

정규 과정: 이 프로젝트의 모든 .bat 은 _common.bat 에서, 모든 .ps1 은 맨 앞에서, 작업 스케줄러
자동 등록(tasks.ensure)은 등록 전에 이것을 거칩니다. 실행할 수 없는 정책(Restricted·AllSigned·
Undefined)이면 RemoteSigned(현재 사용자, 영구)로 켜고, 인터넷에서 받은 표시(MOTW)가 붙은
scripts\\*.ps1 의 차단을 풉니다. 그룹 정책으로 막혀 있으면 바꿀 수 없어 알리기만 합니다.

정책이 어떻든 이 프로젝트가 .ps1 을 부를 때는 -ExecutionPolicy Bypass 를 주므로 우리 작업은
돕니다. 영구 설정은 사람이 .ps1 을 직접 실행(더블클릭·우클릭)할 때 막혀 창이 바로 닫히는 것을
막으려는 것입니다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import get as get_logger

log = get_logger("exec_policy")

GOOD = ("RemoteSigned", "Unrestricted", "Bypass")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "exec_policy.ps1"


def supported() -> bool:
    """Windows 이고 레포에서 실행 중일 때만. SYNCNOTE_NO_TASK_CHECK=1 이면 끔(테스트 — 실제
    정책을 건드리지 않게, 작업 스케줄러 확인과 같은 스위치)."""
    if os.environ.get("SYNCNOTE_NO_TASK_CHECK"):
        return False
    return sys.platform == "win32" and script_path().is_file()


@dataclass
class Policy:
    checked: bool
    before: str = ""
    after: str = ""
    scope: str = ""
    changed: list[str] = field(default_factory=list)
    gpo_blocked: bool = False
    unblocked: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.checked and self.after in GOOD

    def describe(self) -> str:
        if not self.checked:
            return f"확인할 수 없습니다({self.error or '알 수 없는 이유'})"
        text = f"{self.after} ({self.scope})"
        if self.changed:
            text = f"{self.before} → RemoteSigned 로 영구 설정함 ({', '.join(self.changed)})"
        if self.unblocked:
            text += f", 차단 해제: {', '.join(self.unblocked)}"
        if self.gpo_blocked:
            text += " — 그룹 정책으로 막혀 바꿀 수 없음(우리 작업은 Bypass 로 실행되어 돕니다)"
        elif not self.ok:
            text += " — .ps1 을 실행할 수 없음"
        return text


def _powershell() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")     # Windows 환경 변수는 대소문자 무시
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.is_file() else "powershell"


def ps_env() -> dict:
    """Windows PowerShell 5.1 을 부를 환경. PowerShell 7 창에서 시작했으면 PSModulePath 가 7 의
    모듈을 가리켜 5.1 이 Get-ExecutionPolicy 등을 불러오지 못합니다(이 PC 에서 실제로 걸림).
    비우면 5.1 이 자기 기본값을 다시 만듭니다."""
    env = dict(os.environ)
    env.pop("PSModulePath", None)
    env.pop("PSMODULEPATH", None)
    return env


def parse(text: str) -> Policy:
    """exec_policy.ps1 -Json 의 출력 → Policy. (실행과 나눠 테스트)"""
    line = next((ln for ln in reversed(text.strip().splitlines()) if ln.startswith("{")), "")
    try:
        d = json.loads(line)
    except ValueError:
        return Policy(checked=False, error="결과를 읽지 못했습니다")

    def lst(v) -> list[str]:            # ConvertTo-Json 은 원소 하나짜리 배열을 문자열로 냄
        return [v] if isinstance(v, str) and v else list(v or [])

    return Policy(checked=bool(d.get("after")), before=str(d.get("before") or ""),
                  after=str(d.get("after") or ""), scope=str(d.get("after_scope") or ""),
                  changed=lst(d.get("changed")), gpo_blocked=bool(d.get("gpo_blocked")),
                  unblocked=lst(d.get("unblocked")), error=str(d.get("error") or ""))


def run(*, check_only: bool = False) -> Policy:
    """확인하고(check_only 가 아니면) 부족하면 영구적으로 켭니다. 예외를 던지지 않음."""
    if not supported():
        return Policy(checked=False, error="Windows 레포 실행이 아님")
    args = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script_path()), "-Json"] + (["-CheckOnly"] if check_only else [])
    try:
        cp = subprocess.run(args, capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                            env=ps_env(), creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return Policy(checked=False, error=str(exc))
    pol = parse(cp.stdout.decode("utf-8", "replace"))
    if pol.changed or pol.checked and not pol.ok:
        log.warning("PowerShell 실행 정책: %s", pol.describe())
    return pol


def ensure() -> Policy:
    return run(check_only=False)


def status() -> Policy:
    return run(check_only=True)
