"""Playwright 브라우저 받기 — 학교망이 HTTPS 를 중간에서 검사해도.

    <.venv 의 python> -m syncnote.playwright_setup [--browser chromium]

강의실 PC 에서 `playwright install chromium` 이

    Error: self-signed certificate in certificate chain  { code: 'SELF_SIGNED_CERT_IN_CHAIN' }

로 일부만 받고 끝났습니다(일반 Chromium·ffmpeg 는 받고 chrome-headless-shell 은 실패).
학교망 장비가 HTTPS 를 풀어 보고 자기 인증서로 다시 감싸는데, 그 인증서는 Windows
인증서 저장소에는 들어 있어 브라우저·파이썬은 믿지만, Playwright 의 다운로더(Node.js)는
Windows 저장소를 보지 않아 거부합니다.

그래서
  1) 그냥 받아 봄
  2) 인증서 오류면 Windows 인증서 저장소(ROOT·CA)를 PEM 파일로 꺼내
     NODE_EXTRA_CA_CERTS 로 넘겨 한 번 더 받음 — **검증을 끄지 않습니다**
     (NODE_TLS_REJECT_UNAUTHORIZED=0 같은 방법은 쓰지 않음)
  3) 그래도 실패면 종료 코드 1 과 사람이 할 일
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

CERT_ERROR = re.compile(
    r"SELF_SIGNED_CERT_IN_CHAIN|self[- ]signed certificate|UNABLE_TO_GET_ISSUER_CERT|"
    r"UNABLE_TO_VERIFY_LEAF_SIGNATURE|unable to verify the first certificate|"
    r"CERT_UNTRUSTED|certificate has expired", re.I)


def _state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "sync-note" / "state"


def windows_ca_pem(dest: Path | None = None, *,
                   enum: Callable[[str], list] | None = None) -> tuple[Path | None, int]:
    """Windows 인증서 저장소(ROOT·CA)를 PEM 하나로. (경로, 인증서 수). Windows 가 아니면 (None, 0)."""
    enum = enum or getattr(ssl, "enum_certificates", None)
    if enum is None:
        return None, 0
    seen, pems = set(), []
    for store in ("ROOT", "CA"):
        try:
            items = enum(store)
        except (OSError, PermissionError):
            continue
        for der, encoding, _trust in items:
            if encoding != "x509_asn" or der in seen:
                continue
            seen.add(der)
            pems.append(ssl.DER_cert_to_PEM_cert(der))
    if not pems:
        return None, 0
    dest = dest or (_state_dir() / "windows-ca.pem")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(pems), encoding="ascii")
    return dest, len(pems)


def _run_install(browser: str, env: dict, run: Callable) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "playwright", "install", browser]
    try:
        p = run(cmd, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"playwright 를 실행하지 못했습니다: {exc}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _default_run(cmd: list[str], env: dict):
    """출력을 그대로 보여 주면서(진행률) 오류 판정용으로도 모읍니다."""
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(lines), "")


def install(browser: str = "chromium", *, run: Callable | None = None,
            ca: Callable[[], tuple[Path | None, int]] | None = None,
            say: Callable[[str], None] = print) -> int:
    run = run or _default_run
    ca = ca or windows_ca_pem
    env = dict(os.environ)
    rc, out = _run_install(browser, env, run)
    if rc == 0 and not CERT_ERROR.search(out):
        return 0
    if not CERT_ERROR.search(out):
        say(f"\n[X] playwright install {browser} 실패 (종료 코드 {rc}).")
        return rc or 1
    say("\n[!] 인증서 검사에서 막혔습니다(학교·회사망이 HTTPS 를 중간에서 검사하는 경우).")
    pem, n = ca()
    if pem is None:
        say("    Windows 인증서 저장소를 꺼내지 못했습니다.")
        return 1
    say(f"    Windows 인증서 저장소의 인증서 {n}개를 Playwright 에도 믿게 하고 다시 받습니다.")
    say(f"    ({pem}, 검증은 끄지 않습니다)")
    env["NODE_EXTRA_CA_CERTS"] = str(pem)
    rc, out = _run_install(browser, env, run)
    if rc == 0 and not CERT_ERROR.search(out):
        say("    완료.")
        return 0
    say("\n[X] 그래도 받지 못했습니다. 이 망의 검사용 루트 인증서가 Windows 에 설치돼 있지 않은 것 같습니다.")
    say("    - 학교 전산실에 그 인증서를 받아 '신뢰할 수 있는 루트 인증 기관' 에 설치하고 다시 실행하거나")
    say("    - 다른 망(휴대폰 테더링 등)에서 scripts\\setup.bat 을 다시 실행하세요.")
    say("    수집은 일반 Chromium 만 받아져 있어도 됩니다(headless shell 이 없으면 그것으로 띄움).")
    return rc or 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m syncnote.playwright_setup")
    ap.add_argument("--browser", default="chromium")
    ap.add_argument("--export-ca", action="store_true",
                    help="Windows 인증서 저장소를 PEM 으로 꺼내기만 (경로 출력)")
    a = ap.parse_args(argv)
    if a.export_ca:
        pem, n = windows_ca_pem()
        print(f"{pem} ({n}개)" if pem else "Windows 인증서 저장소가 없습니다(Windows 전용).")
        return 0 if pem else 1
    return install(a.browser)


if __name__ == "__main__":
    raise SystemExit(main())
