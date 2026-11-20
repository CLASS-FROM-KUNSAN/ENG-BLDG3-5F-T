"""편집기를 닫은 뒤에 패키지를 설치하고, 끝나면 편집기를 다시 엽니다.

시간표 편집기는 .venv 의 PySide6 등으로 돌고 있어서, 그 패키지를 실행 중에
바꾸면 Windows 가 파일을 잠가 pip 가 실패합니다(WinError 5/32). 그래서 편집기가
이 스크립트를 새 콘솔 창에 띄우고 스스로 닫히면, 여기서 편집기가 끝나기를
기다렸다가 설치합니다.

    python -m syncnote.deferred_install --pid 1234 [--force] [--relaunch] -- 패키지줄...

  --pid       기다릴 편집기 프로세스
  --force     깨진 설치(import 실패)를 다시 깔기: --force-reinstall --no-deps
  --relaunch  설치가 성공하면 scripts/timetable_edit.bat 로 편집기를 다시 엶

표준 라이브러리만 씁니다(설치할 패키지를 이 스크립트가 잡고 있으면 안 되므로).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def wait_pid(pid: int, timeout: float = 120.0) -> bool:
    """pid 프로세스가 끝날 때까지(최대 timeout 초). 끝났으면 True."""
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            return True                  # 이미 없음
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    end = time.monotonic() + timeout     # pragma: no cover
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.3)
    return False


def parse(argv: list[str]) -> tuple[int, bool, bool, list[str]]:
    pid, force, relaunch, lines = 0, False, False, []
    it = iter(argv)
    for a in it:
        if a == "--pid":
            pid = int(next(it))
        elif a == "--force":
            force = True
        elif a == "--relaunch":
            relaunch = True
        elif a == "--":
            lines = list(it)
            break
    return pid, force, relaunch, lines


def pip_args(lines: list[str], force: bool) -> list[str]:
    flags = ["--force-reinstall", "--no-deps"] if force else []
    return [sys.executable, "-m", "pip", "install", *flags, *lines]


def main(argv: list[str] | None = None) -> int:
    pid, force, relaunch, lines = parse(list(sys.argv[1:] if argv is None else argv))
    print("시간표 편집기가 닫히기를 기다립니다...", flush=True)
    if not wait_pid(pid):
        print("편집기가 닫히지 않았습니다. 편집기를 닫은 뒤 이 창을 다시 여세요.")
        input("엔터를 누르면 닫습니다.")
        return 2
    if not lines:
        print("설치할 패키지가 없습니다.")
        return 0
    print("설치합니다: " + " ".join(lines), flush=True)
    rc = subprocess.call(pip_args(lines, force))
    if rc == 0 and relaunch:
        print("설치를 마쳤습니다. 편집기를 다시 엽니다.", flush=True)
        bat = REPO_ROOT / "scripts" / "timetable_edit.bat"
        subprocess.Popen(["cmd.exe", "/d", "/c", str(bat)] if os.name == "nt"
                         else [sys.executable, "-m", "syncnote", "timetable", "edit"],
                         cwd=str(REPO_ROOT))
        time.sleep(1)
        return 0
    print(f"\npip 가 실패했습니다(종료 코드 {rc}). 위 오류를 확인하세요.")
    input("엔터를 누르면 닫습니다.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
