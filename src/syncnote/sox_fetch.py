"""SoX 설치(필수 녹음 도구). setup.bat 과 doctor 가 씁니다.

    python -m syncnote.sox_fetch [--dest tools]

녹음 방법 사슬(soundcard → ffmpeg → SoX → arecord)의 세 번째 방법이고, 소리가 날 때만 녹음하는
터미널 녹음(--vad)의 기본 도구입니다. ffmpeg 처럼 원인을 보며 차례로 시도합니다.

    0) 이미 쓸 수 있는 SoX(환경변수 SOX · PATH · tools\\sox · winget 이 둔 곳)가 있으면 끝
    1) winget install ChrisBagwell.SoX
    2) SourceForge 공식 zip 을 직접 — 크기·SHA-256 을 winget 매니페스트에 적힌 값과 대조하고,
       압축을 임시 폴더에 풀어 실행이 되는지 본 뒤 tools\\sox 와 바꿔 끼웁니다(정규화 롤백:
       도중에 실패하면 쓰던 tools\\sox 를 그대로 되돌리고, 받다 만 파일·임시 폴더는 지움).
       주소는 https 이고 sourceforge.net 과 그 하위 주소(미러)로 가는 리다이렉트만 따라갑니다.

Windows 가 아니면 설치하지 않고 설치 명령을 알려 줍니다(macOS brew, 리눅스 apt). arecord 는
리눅스 기본 도구(alsa-utils)라 리눅스에서만 필수입니다.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SOX_VERSION = "14.4.2"
SOX_URL = ("https://sourceforge.net/projects/sox/files/sox/14.4.2/"
           "sox-14.4.2-win32.zip/download")
# winget ChrisBagwell.SoX 14.4.2 매니페스트의 InstallerSha256 과 같은 값(2026-11-23 대조:
# 직접 받은 파일 2,651,143 바이트의 SHA-256 이 이 값과 일치).
SOX_SHA256 = "8072cc147cf1a3b3713b8b97d6844bb9389e211ab9e1101e432193fad6ae6662"
SOX_SIZE = 2_651_143
WINGET_ID = "ChrisBagwell.SoX"
WINGET_ARGS = ["install", "--id", WINGET_ID, "-e", "--silent",
               "--accept-package-agreements", "--accept-source-agreements"]
USER_AGENT = "syncnote-setup (SoX installer)"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

LINUX_HINT = "sudo apt install sox libsox-fmt-pulse alsa-utils"
MAC_HINT = "brew install sox"


def _say(msg: str) -> None:
    print(msg, flush=True)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 찾기
# ---------------------------------------------------------------------------
def candidates(*, root: Path | None = None, env: dict | None = None,
               which: Callable[[str], str | None] = shutil.which) -> list[Path]:
    env = os.environ if env is None else env
    out: list[Path] = []
    if env.get("SOX"):
        out.append(Path(env["SOX"]))
    w = which("sox")
    if w:
        out.append(Path(w))
    root = root or repo_root()
    out += sorted((root / "tools" / "sox").glob("**/sox.exe"))
    local = env.get("LOCALAPPDATA")
    if local:
        wg = Path(local) / "Microsoft" / "WinGet"
        out.append(wg / "Links" / "sox.exe")
        out += sorted((wg / "Packages").glob(f"{WINGET_ID}*/**/sox.exe"))
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def works(path: Path | str, *, run: Callable = subprocess.run) -> bool:
    """실행해서 SoX 가 맞는지(버전 줄)."""
    try:
        p = run([str(path), "--version"], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and "sox" in ((p.stdout or "") + (p.stderr or "")).lower()


def find_sox(*, root: Path | None = None, run: Callable = subprocess.run,
             which: Callable = shutil.which, env: dict | None = None,
             check: bool = True) -> str | None:
    """쓸 수 있는 SoX. 없으면 None. check=False 면 파일이 있는지만 봅니다(빠름)."""
    for p in candidates(root=root, env=env, which=which):
        if Path(p).exists() and (not check or works(p, run=run)):
            return str(p)
    return None


# ---------------------------------------------------------------------------
# 설치
# ---------------------------------------------------------------------------
@dataclass
class Result:
    ok: bool
    sox: str = ""
    tried: list[tuple[str, str]] = field(default_factory=list)   # (단계, 결과·이유)


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def host_ok(url: str) -> bool:
    from urllib.parse import urlsplit

    u = urlsplit(str(url))
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "sourceforge.net" or host.endswith(".sourceforge.net"))


def _check_host(url: str) -> None:
    if not host_ok(url):
        raise OSError(f"허용 밖 주소로 가려 함: {url.split('?')[0]}")


def _open(url: str, timeout: float = 60):
    _check_host(url)
    fetch = urllib.request.build_opener(_GuardRedirect).open   # 네트워크 열기(파일 open 이 아님)
    return fetch(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def download(dest: Path, *, opener: Callable = _open, url: str | None = None,
             sha256: str | None = None, size: int | None = None) -> Path:
    """dest 로 받고 크기·SHA-256 확인. 다르면 지우고 예외(받다 만 .part 도 지움)."""
    url = url or SOX_URL
    sha256 = sha256 or SOX_SHA256
    size = SOX_SIZE if size is None else size
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    n = 0
    try:
        with opener(url) as resp, part.open("wb") as fh:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
                n += len(chunk)
        if size and n != size:
            raise OSError(f"크기가 다릅니다(받음 {n} / 예상 {size}) — 웹 페이지가 왔거나 끊겼습니다")
        if h.hexdigest() != sha256:
            raise OSError(f"SHA-256 이 다릅니다(받음 {h.hexdigest()[:16]}…) — 쓰지 않습니다")
        part.replace(dest)
        return dest
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def extract(zip_path: Path, into: Path) -> Path:
    """zip 을 풀고 sox.exe 경로. 바깥을 가리키는 경로가 있으면 거부."""
    into.mkdir(parents=True, exist_ok=True)
    base = into.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for m in z.namelist():
            t = (base / m).resolve()
            if t != base and base not in t.parents:
                raise OSError(f"압축 안 경로가 바깥을 가리킴: {m}")
        z.extractall(base)
    hits = sorted(base.glob("**/sox.exe"))
    if not hits:
        raise OSError("압축 안에 sox.exe 가 없습니다")
    return hits[0]


def swap_in(new_dir: Path, dest: Path) -> None:
    """dest 를 new_dir 로 바꿉니다. 도중에 실패하면 쓰던 것을 되돌립니다."""
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    had = dest.exists()
    if had:
        dest.replace(old)
    try:
        shutil.move(str(new_dir), str(dest))
    except OSError:
        shutil.rmtree(dest, ignore_errors=True)
        if had:
            old.replace(dest)                       # 롤백
        raise
    shutil.rmtree(old, ignore_errors=True)


def install_zip(tools: Path, *, opener: Callable = _open, run: Callable = subprocess.run,
                say: Callable[[str], None] = _say) -> str:
    """공식 zip → 확인 → tools\\sox. 성공하면 sox.exe 경로."""
    work = tools / ".sox-install"
    shutil.rmtree(work, ignore_errors=True)
    try:
        say(f"SoX {SOX_VERSION} 를 SourceForge 에서 받습니다(약 2.6MB)...")
        z = download(work / "sox.zip", opener=opener)
        exe = extract(z, work / "new")
        if not works(exe, run=run):
            raise OSError("받은 sox.exe 가 실행되지 않습니다")
        new_dir = exe.parent
        swap_in(new_dir, tools / "sox")
        return str(tools / "sox" / "sox.exe")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def winget_install(*, which: Callable = shutil.which, run: Callable = subprocess.run,
                   say: Callable[[str], None] = _say) -> str:
    """winget 으로 설치. 실패 이유를 돌려줌(성공은 다시 찾아서 판정)."""
    exe = which("winget")
    if not exe:
        return "winget 이 없음"
    say("winget 으로 SoX 를 설치합니다...")
    try:
        p = run([exe, *WINGET_ARGS], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"winget 실행 실패: {exc}"
    return "" if p.returncode == 0 else f"winget 종료 코드 {p.returncode}"


def install(tools: Path | None = None, *, platform: str = sys.platform,
            run: Callable = subprocess.run, which: Callable = shutil.which,
            opener: Callable = _open, env: dict | None = None,
            say: Callable[[str], None] = _say) -> Result:
    root = repo_root() if tools is None else Path(tools).parent
    tools = Path(tools) if tools is not None else root / "tools"
    r = Result(False)
    found = find_sox(root=root, run=run, which=which, env=env)
    if found:
        r.ok, r.sox = True, found
        r.tried.append(("찾기", f"있음: {found}"))
        return r
    r.tried.append(("찾기", "없음"))
    if platform != "win32":
        hint = MAC_HINT if platform == "darwin" else LINUX_HINT
        r.tried.append(("설치", f"이 OS 는 직접 설치하세요: {hint}"))
        return r
    why = winget_install(which=which, run=run, say=say)
    found = find_sox(root=root, run=run, which=which, env=env)
    if found:
        r.ok, r.sox = True, found
        r.tried.append(("winget", "설치됨"))
        return r
    r.tried.append(("winget", why or "설치했다는데 찾지 못함"))
    try:
        r.sox = install_zip(tools, opener=opener, run=run, say=say)
        r.ok = True
        r.tried.append(("공식 zip", f"설치됨: {r.sox}"))
    except Exception as exc:
        r.tried.append(("공식 zip", str(exc) or type(exc).__name__))
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SoX 설치(필수 녹음 도구)")
    ap.add_argument("--dest", help="tools 폴더(기본: 레포의 tools)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = install(Path(a.dest) if a.dest else None)
    for step, what in r.tried:
        _say(f"  - {step}: {what}")
    _say(f"SoX: {r.sox}" if r.ok else "SoX 를 설치하지 못했습니다. 위 이유를 보세요.")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
