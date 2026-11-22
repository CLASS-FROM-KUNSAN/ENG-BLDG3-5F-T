"""ffmpeg 를 실제 릴리스 목록에서 받아 tools\\ffmpeg 에 둡니다 — 실패하면 이전 버전으로 롤백.

    python -m syncnote.ffmpeg_fetch [--dest tools] [--max 6] [--force] [--dry-run]

install_ffmpeg.bat 이 winget 이 없거나 실패했을 때 부릅니다. 고정 URL 하나만 믿지
않고, winget 의 Gyan.FFmpeg 가 실제로 받아 오는 GitHub 저장소(GyanD/codexffmpeg)의
릴리스 목록을 읽어 새 것부터 차례로 시도합니다.

  후보 순서   정식 버전(9.1, 9.0 …) 새 것부터 → 날짜별 git 빌드 새 것부터
              → (목록을 못 읽으면) gyan.dev 의 release-essentials
  후보마다    1) 내려받기 — 끊기면 2·4·8 초 쉬고 다시(일시적 오류만)
              2) SHA-256 — 릴리스가 알려 준 digest 와 다르면 버림
              3) 압축 풀기 — 레포 밖으로 나가는 경로는 거부
              4) 기능 확인 — 녹화에 꼭 필요한 gdigrab·libx264·aac 가 있어야 함
              하나라도 실패하면 그 후보를 지우고 **한 단계 이전 릴리스로 롤백**
  바꿔 끼우기 tools\\ffmpeg 를 tools\\ffmpeg.old 로 옮긴 뒤 새 것을 넣고, 도중에
              실패하면 옛것을 되돌려 놓습니다(쓰던 ffmpeg 를 잃지 않게).
  디스크 부족 다른 후보도 같으니 바로 멈춥니다.

표준 라이브러리만 씁니다(설치 단계라 requests 가 아직 없을 수 있음). 프록시는
urllib 가 HTTPS_PROXY 환경변수를 그대로 따릅니다.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASES_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases?per_page=100"
RELEASE_TAG_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/{tag}"
GYAN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
USER_AGENT = "syncnote-setup"
ASSET_SUFFIX = "-essentials_build.zip"      # 녹화에 필요한 것은 다 있고 full 의 절반 크기
BACKOFF = (2, 4, 8)
# 녹화에 꼭 필요한 것. 화면 캡처(gdigrab)는 Windows 녹화기에서만 씁니다.
NEEDED = {"gdigrab": "-devices", "libx264": "-encoders", "aac": "-encoders"}
if os.name != "nt":
    NEEDED = {k: v for k, v in NEEDED.items() if k != "gdigrab"}


def _say(msg: str) -> None:
    """바로 내보냅니다 — bat 창에서 pip·다운로드 출력과 순서가 섞이지 않게."""
    print(msg, flush=True)


class Fatal(Exception):
    """다른 후보로 넘어가도 소용없는 실패(디스크 부족 등)."""


class CandidateFailed(Exception):
    """이 후보만 못 씀 — 이전 릴리스로 롤백.

    kind: net(내려받기) · hash(해시 다름) · feature(기능 없음) · other
    """

    def __init__(self, msg: str, kind: str = "other") -> None:
        super().__init__(msg)
        self.kind = kind


@dataclass
class Candidate:
    name: str
    url: str
    version: str
    sha256: str = ""          # 비어 있으면 확인할 해시가 없음
    size: int = 0
    stable: bool = False
    published: str = ""
    sources: tuple[str, ...] = ()        # 이 버전을 알려 준 출처(api·gh·git)
    verified_by: tuple[str, ...] = ()    # 해시를 준 출처(api·gh·gyan.dev)
    synthesized: bool = False            # git 에서 와서 주소를 태그로 만들었음


@dataclass
class Result:
    ok: bool
    ffmpeg: str = ""
    used: Candidate | None = None
    tried: list[tuple[str, str]] = field(default_factory=list)   # (버전, 실패 이유)
    kinds: list[str] = field(default_factory=list)                # tried 와 같은 순서
    list_failed: bool = False                                     # 릴리스 목록을 못 읽음
    sources: list[str] = field(default_factory=list)              # 출처별 조회 결과


# ---------------------------------------------------------------------------
# 후보 목록
# ---------------------------------------------------------------------------
def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.split("."))


def candidates(releases: list[dict]) -> list[Candidate]:
    """GitHub 릴리스 JSON -> 시도할 순서대로(순수 함수)."""
    out: list[Candidate] = []
    for rel in releases or []:
        if rel.get("draft"):
            continue
        tag = str(rel.get("tag_name", ""))
        for a in rel.get("assets") or []:
            name = str(a.get("name", ""))
            if not name.endswith(ASSET_SUFFIX):
                continue
            digest = str(a.get("digest") or "")
            out.append(Candidate(
                name=name, url=str(a.get("browser_download_url", "")), version=tag,
                sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
                size=int(a.get("size") or 0),
                stable=bool(re.fullmatch(r"\d+(\.\d+)+", tag)) and not rel.get("prerelease"),
                published=str(rel.get("published_at", ""))))
    stable = sorted((c for c in out if c.stable), key=lambda c: _version_key(c.version), reverse=True)
    nightly = sorted((c for c in out if not c.stable), key=lambda c: c.published, reverse=True)
    return [c for c in stable + nightly if c.url]


# 받을 수 있는 곳. GitHub 릴리스 파일은 release-assets(예전엔 objects).githubusercontent.com
# 으로, gyan.dev 는 같은 호스트의 버전별 파일로 리다이렉트됩니다(2026-11 실제 확인).
ALLOWED_HOSTS = frozenset({
    "github.com", "api.github.com", "release-assets.githubusercontent.com",
    "objects.githubusercontent.com", "www.gyan.dev", "gyan.dev"})


class HostNotAllowed(Exception):
    """허용 밖 주소 — 다른 후보로(다시 해도 같음)."""


def host_ok(url: str) -> bool:
    from urllib.parse import urlsplit

    u = urlsplit(str(url))
    return u.scheme == "https" and (u.hostname or "").lower() in ALLOWED_HOSTS


def _check_host(url: str) -> None:
    if not host_ok(url):
        from urllib.parse import urlsplit

        raise HostNotAllowed(f"허용 밖 주소: {urlsplit(str(url)).hostname or url}")


class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트 한 걸음마다 허용 주소인지 봅니다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardRedirect)
_urlopen = _OPENER.open        # 네트워크 열기(파일 open 이 아님)


def _open(url: str, timeout: float = 30, headers: dict | None = None):
    _check_host(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json",
                                               **(headers or {})})
    return _urlopen(req, timeout=timeout)


def _api_get(url: str, opener: Callable = _open):
    """GitHub API GET -> ApiResult(데이터, 남은 한도). 한도 초과·막힘은 따로 가림."""
    from .release_sources import ApiResult, api_error

    try:
        with opener(url) as r:
            data = json.loads(r.read().decode("utf-8"))
            h = getattr(r, "headers", {}) or {}
    except urllib.error.HTTPError as exc:
        special = api_error(exc.code, exc.headers)
        if special is not None:
            raise special from exc
        raise

    def num(k):
        v = str(h.get(k) or "")
        return int(v) if v.isdigit() else None
    return ApiResult(data, num("X-RateLimit-Remaining"), num("X-RateLimit-Limit"))


def fetch_releases(opener: Callable = _open):
    return _api_get(RELEASES_API, opener)


def fetch_release_tag(tag: str, opener: Callable = _open):
    from urllib.parse import quote

    return _api_get(RELEASE_TAG_API.format(tag=quote(tag, safe="")), opener)


def gyan_sha(opener: Callable = _open) -> str:
    """gyan.dev 가 release-essentials 옆에 둔 .sha256 (최신 정식 버전과 같은 빌드)."""
    from .release_sources import norm_sha

    try:
        with opener(GYAN_URL + ".sha256", timeout=15) as r:
            return norm_sha(r.read().decode("ascii", "replace"))
    except Exception:  # noqa: BLE001 — 없으면 기능 확인만으로 판단
        return ""


def gyan_candidate(opener: Callable = _open, sha: str | None = None) -> Candidate:
    """목록을 못 읽었을 때의 마지막 후보. 해시는 gyan.dev 가 옆에 둔 .sha256 으로."""
    sha = gyan_sha(opener) if sha is None else sha
    return Candidate(name="ffmpeg-release-essentials.zip", url=GYAN_URL, version="gyan.dev",
                     sha256=sha, stable=True, sources=("gyan.dev",),
                     verified_by=("gyan.dev",) if sha else ())


def to_candidate(e) -> Candidate:
    from .release_sources import asset_name

    return Candidate(name=asset_name(e.tag), url=e.url, version=e.tag, sha256=e.sha256,
                     size=e.size, stable=e.kind == "stable", published=e.published,
                     sources=tuple(sorted(e.sources)), verified_by=tuple(e.verified_by),
                     synthesized=e.synthesized)


def head_size(url: str, opener: Callable = _open) -> int:
    """받기 전에 파일이 실제로 있는지(만든 주소일 때). 없으면 CandidateFailed."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        if opener is _open:
            _check_host(url)
            with _urlopen(req, timeout=30) as r:
                return int(r.headers.get("Content-Length") or 0)
        with opener(url, timeout=30) as r:                       # 테스트용 가짜
            return int(r.headers.get("Content-Length") or 0)
    except HostNotAllowed as exc:
        raise CandidateFailed(str(exc)) from exc
    except urllib.error.HTTPError as exc:
        raise CandidateFailed(f"태그로 만든 주소에 파일이 없음 (HTTP {exc.code})") from exc
    except OSError as exc:
        raise CandidateFailed(f"파일 확인 실패: {exc}", "net") from exc


def gh_download(c: Candidate, work: Path, *, run: Callable | None = None) -> Path:
    """urllib 가 안 될 때: 로그인한 gh 로 같은 첨부 파일을 받습니다."""
    from . import release_sources as RS

    run = run or RS._run
    exe, why = RS.gh_ready(run)
    if not exe:
        raise CandidateFailed(f"gh 로도 못 받음: {why}", "net")
    p = run([exe, "release", "download", c.version, "-R", RS.REPO, "-p", c.name,
             "-D", str(work), "--clobber"], timeout=1800)
    out = work / c.name
    if p.returncode != 0 or not out.exists():
        raise CandidateFailed(f"gh release download 실패: {(p.stderr or '').strip()[:160]}", "net")
    if c.sha256 and hashlib.sha256(out.read_bytes()).hexdigest() != c.sha256:
        out.unlink(missing_ok=True)
        raise CandidateFailed("SHA-256 이 릴리스에 적힌 값과 다름 (gh)", "hash")
    return out


# ---------------------------------------------------------------------------
# 한 후보 처리
# ---------------------------------------------------------------------------
def _transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429) or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _raise_if_disk_full(exc: OSError) -> None:
    if exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == 112:
        raise Fatal("디스크 공간이 부족합니다. 공간을 비운 뒤 다시 실행하세요.") from exc


def download(c: Candidate, dest: Path, *, opener: Callable = _open,
             sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = None) -> Path:
    """dest 로 받고 크기·SHA-256 을 확인합니다.

    끊기면 받은 데까지(.part) 남겨 두고 Range 로 이어받습니다. 서버가 이어받기를
    무시하고 처음부터 보내면(200) 처음부터 다시 씁니다. 일시적 오류만 2·4·8 초 뒤 다시.
    """
    say = say or _say
    part = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(len(BACKOFF) + 1):
        offset = part.stat().st_size if part.exists() else 0
        if c.size and offset >= c.size:
            part.unlink(missing_ok=True)                     # 다 받았거나 더 큼 -> 처음부터
            offset = 0
        try:
            if offset:
                resp = opener(c.url, timeout=60, headers={"Range": f"bytes={offset}-"})
            else:
                resp = opener(c.url, timeout=60)
            with resp as r:
                final = getattr(r, "url", None)
                if final:
                    _check_host(final)                       # 리다이렉트 끝 주소
                status = getattr(r, "status", None) or getattr(r, "code", None) or 200
                if offset and status != 206:
                    say("      서버가 이어받기를 받지 않아 처음부터 받습니다")
                    offset = 0
                elif offset:
                    say(f"      이어받기: {offset / 1e6:,.0f} MB 부터")
                total = c.size or (offset + int(r.headers.get("Content-Length") or 0))
                with (part.open("ab") if offset else part.open("wb")) as f:
                    got, shown = offset, -1
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        pct = got * 100 // total if total else -1
                        if pct >= 0 and pct // 20 != shown:
                            shown = pct // 20
                            say(f"      {pct:3d}%  {got / 1e6:,.0f} / {total / 1e6:,.0f} MB")
            break
        except HostNotAllowed as exc:
            part.unlink(missing_ok=True)
            raise CandidateFailed(str(exc)) from exc
        except OSError as exc:
            if not isinstance(exc, urllib.error.URLError):
                _raise_if_disk_full(exc)
            if attempt < len(BACKOFF) and _transient(exc):
                say(f"      내려받기 끊김({exc}) — {BACKOFF[attempt]}초 뒤 이어서")
                sleep(BACKOFF[attempt])
                continue                                     # .part 는 남겨 이어받기
            if not _transient(exc):
                part.unlink(missing_ok=True)
            raise CandidateFailed(f"내려받기 실패: {exc}", "net") from exc
    size = part.stat().st_size
    if c.size and size != c.size:
        part.unlink(missing_ok=True)
        raise CandidateFailed(f"크기가 릴리스에 적힌 값과 다름 ({size:,} / {c.size:,} 바이트)",
                              "hash")
    if c.sha256 and _sha256_file(part) != c.sha256:
        part.unlink(missing_ok=True)
        raise CandidateFailed("SHA-256 이 릴리스에 적힌 값과 다름", "hash")
    part.replace(dest)
    return dest


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CACHE_NAME = ".ffmpeg_releases.json"   # 합친 릴리스 목록(tools 안)
DISK_FACTOR = 2.5             # zip + 압축 푼 것 + 바꿔 끼우는 동안의 옛것


def check_disk(dest: Path, size: int, *, free: Callable[[Path], int] | None = None) -> None:
    """받기 전에 남은 공간을 봅니다. 모자라면 Fatal(다른 후보도 같음)."""
    if not size:
        return
    free = free or (lambda p: shutil.disk_usage(p).free)
    need = int(size * DISK_FACTOR)
    have = free(dest)
    if have < need:
        raise Fatal(f"디스크 공간이 부족합니다 (필요 약 {need / 1e9:.1f}GB, "
                    f"남음 {have / 1e9:.1f}GB). 공간을 비운 뒤 다시 실행하세요.")


def extract(zip_path: Path, into: Path) -> Path:
    """압축을 풀고 ffmpeg 폴더(그 안에 bin\\ffmpeg.exe)를 돌려줍니다."""
    into.mkdir(parents=True, exist_ok=True)
    base = into.resolve()
    try:
        with zipfile.ZipFile(zip_path) as z:
            for m in z.namelist():
                target = (base / m).resolve()
                if base != target and base not in target.parents:
                    raise CandidateFailed(f"압축 안 경로가 바깥을 가리킴: {m}")
            z.extractall(base)
    except zipfile.BadZipFile as exc:
        raise CandidateFailed("zip 이 깨짐") from exc
    except OSError as exc:
        _raise_if_disk_full(exc)
        raise CandidateFailed(f"압축 풀기 실패: {exc}") from exc
    hits = sorted(base.glob("*/bin/ffmpeg.exe")) or sorted(base.glob("bin/ffmpeg.exe"))
    if not hits:
        raise CandidateFailed("압축 안에 bin\\ffmpeg.exe 가 없음")
    return hits[0].parent.parent


def missing_features(ffmpeg: Path, *, run: Callable = subprocess.run) -> list[str]:
    """녹화에 필요한데 이 ffmpeg 빌드에 없는 것. 실행조차 안 되면 ['실행 안 됨']."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    seen: dict[str, str] = {}
    for opt in sorted(set(NEEDED.values())):
        try:
            p = run([str(ffmpeg), "-hide_banner", opt], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=30, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            return ["실행 안 됨"]
        seen[opt] = (p.stdout or "") + (p.stderr or "")
    return [name for name, opt in NEEDED.items()
            if not re.search(rf"(^|\s){re.escape(name)}(\s|$)", seen[opt], re.M)]


def swap_in(new_dir: Path, dest: Path) -> Path:
    """dest\\ffmpeg 를 new_dir 로 바꿉니다. 도중에 실패하면 옛것을 되돌립니다."""
    final, old = dest / "ffmpeg", dest / "ffmpeg.old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    had_old = final.exists()
    if had_old:
        try:
            final.replace(old)
        except OSError as exc:              # 녹화 중이라 ffmpeg.exe 가 잠김 등 — 후보 탓 아님
            _raise_if_disk_full(exc)
            raise Fatal(f"쓰던 tools\\ffmpeg 를 옮기지 못했습니다({exc}). "
                        "녹화·송출을 끈 뒤 다시 실행하세요.") from exc
    try:
        shutil.move(str(new_dir), str(final))
    except OSError as exc:
        if final.exists():
            shutil.rmtree(final, ignore_errors=True)
        if had_old:
            old.replace(final)                      # 롤백: 쓰던 것을 그대로
        _raise_if_disk_full(exc)
        raise CandidateFailed(f"tools\\ffmpeg 로 옮기지 못함: {exc}") from exc
    if had_old:
        shutil.rmtree(old, ignore_errors=True)
    return final / "bin" / "ffmpeg.exe"


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------
def install(dest: Path, *, max_tries: int = 6, force: bool = False,
            opener: Callable = _open, run: Callable = subprocess.run,
            sleep: Callable[[float], None] = time.sleep,
            say: Callable[[str], None] = None, allow_unverified: bool = False,
            tool_run: Callable | None = None,
            disk_free: Callable[[Path], int] | None = None) -> Result:
    say = say or _say
    dest = Path(dest)
    current = dest / "ffmpeg" / "bin" / "ffmpeg.exe"
    if current.exists() and not force and not missing_features(current, run=run):
        say(f"이미 쓸 수 있는 ffmpeg 가 있습니다: {current}")
        return Result(True, str(current))

    from . import release_sources as RS

    shas: dict[str, str] = {}

    def borrowed() -> str:
        shas.setdefault("gyan", gyan_sha(opener))
        return shas["gyan"]

    cache_path = dest / CACHE_NAME
    cache = RS.load_cache(cache_path)
    entries, reports = RS.collect(api=lambda: fetch_releases(opener), gyan_sha=borrowed,
                                  api_tag=lambda tag: fetch_release_tag(tag, opener),
                                  run=tool_run or RS._run, allow_unverified=allow_unverified,
                                  max_tries=max_tries, cache=cache,
                                  save=lambda book: RS.save_cache(cache_path, book, cache))
    lines = [f"{r.name} {'됨' if r.ok else '안 됨'} — {r.note}" for r in reports]
    for ln in lines:
        say("출처: " + ln)
    listing_failed = not any(r.ok for r in reports if r.name in ("API", "gh", "git", "저장된 목록"))
    cands = [to_candidate(e) for e in entries][:max_tries]
    if cands:
        say(f"후보 {len(cands)}개: " + ", ".join(
            c.version + ("" if c.sha256 else "(해시 없음)") for c in cands))
    else:
        say("[!] 쓸 수 있는 릴리스 후보가 없습니다. gyan.dev 로 시도합니다.")
    cands.append(gyan_candidate(opener, sha=borrowed()))

    result = Result(False, list_failed=listing_failed, sources=lines)
    dest.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(cands):
        if i:
            say(f"  -> 이전 릴리스로 롤백: {c.version}")
        size = f" ({c.size / 1e6:,.0f} MB)" if c.size else ""
        say(f"[{i + 1}/{len(cands)}] {c.version}  {c.name}{size}")
        work = Path(tempfile.mkdtemp(prefix="_ffmpeg_", dir=dest))
        try:
            if c.synthesized and not c.sha256:
                size = head_size(c.url, opener)
                say(f"      태그로 만든 주소 확인: {size / 1e6:,.0f} MB (해시 없음)")
                c.size = c.size or size
            check_disk(dest, c.size, free=disk_free)
            # 해시가 있으면 같은 파일임이 보장되니 실행을 넘어 이어받습니다(_downloads).
            # 해시가 없으면 파일이 바뀌었을 수 있어 이번 실행 안에서만.
            if c.sha256:
                keep = dest / "_downloads"
                keep.mkdir(parents=True, exist_ok=True)
                target = keep / f"{c.sha256[:12]}-{c.name}"
            else:
                target = work / c.name
            try:
                if c.sha256 and target.exists() and _sha256_file(target) == c.sha256:
                    say("      지난번에 다 받아 둔 파일을 씁니다(해시 통과)")
                    z = target
                else:
                    z = download(c, target, opener=opener, sleep=sleep, say=say)
            except CandidateFailed as exc:
                if exc.kind != "net" or c.version == "gyan.dev":
                    raise
                say(f"      [!] {exc} — gh 로 받아 봅니다")
                z = gh_download(c, work, run=tool_run)
            by = "·".join(c.verified_by)
            say("      해시 확인 " + (f"통과 ({by})" if c.sha256 else "생략(해시 없음)"))
            exe = _install_zip(z, work, dest, run=run)
            shutil.rmtree(dest / "_downloads", ignore_errors=True)       # 다 됐으니 받은 것 정리
            say(f"완료: {exe}  ({c.version})")
            result.ok, result.ffmpeg, result.used = True, str(exe), c
            return result
        except CandidateFailed as exc:
            say(f"      [X] {exc}")
            result.tried.append((c.version, str(exc)))
            result.kinds.append(exc.kind)
            if exc.kind != "net" and c.sha256:           # 다시 써도 같은 실패 -> 받은 것 지움
                for left in (dest / "_downloads").glob(f"{c.sha256[:12]}-*"):
                    left.unlink(missing_ok=True)
        except Fatal as exc:
            say(f"[X] {exc}")
            result.tried.append((c.version, str(exc)))
            result.kinds.append("fatal")
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return result


def _install_zip(z: Path, work: Path, dest: Path, *, run: Callable = subprocess.run) -> Path:
    """받았거나 가져온 zip 하나: 압축 풀기 -> 기능 확인 -> tools\\ffmpeg 로 바꿔 끼우기."""
    folder = extract(z, work / "x")
    lack = missing_features(folder / "bin" / "ffmpeg.exe", run=run)
    if lack:
        raise CandidateFailed("녹화에 필요한 기능 없음: " + ", ".join(lack), "feature")
    return swap_in(folder, dest)


def install_from_zip(z: Path, dest: Path, *, sha256: str = "", run: Callable = subprocess.run,
                     say: Callable[[str], None] = None) -> str:
    """오프라인: 사용자가 USB 등으로 가져온 zip. 쓸 수 있게 되면 경로, 아니면 ''."""
    say = say or _say
    dest = Path(dest)
    say(f"가져온 zip 으로 설치합니다: {z}")
    where = "적어 준 값"
    if not sha256:                      # 저장된 목록에서 파일 이름으로 찾기
        from .release_sources import load_cache

        cache = load_cache(dest / CACHE_NAME)
        found, tag = cache.sha_for_name(Path(z).name) if cache else ("", "")
        if found:
            sha256, where = found, f"저장된 목록의 {tag}"
        else:
            say("      해시를 모릅니다(저장된 목록에 없는 이름) — 기능 확인으로만 판단")
    if sha256:
        h = _sha256_file(Path(z))
        if h != sha256.lower():
            say(f"      [X] SHA-256 이 {where}과 다릅니다")
            return ""
        say(f"      해시 확인 통과 ({where})")
    dest.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="_ffmpeg_", dir=dest))
    try:
        exe = _install_zip(Path(z), work, dest, run=run)
    except (CandidateFailed, Fatal) as exc:
        say(f"      [X] {exc}")
        return ""
    finally:
        shutil.rmtree(work, ignore_errors=True)
    say(f"완료: {exe}")
    return str(exe)


def offline_zips(*folders: Path) -> list[Path]:
    """가져다 둔 ffmpeg zip 들(새 것부터). 이름에 ffmpeg 가 든 .zip 만."""
    out: list[Path] = []
    for d in folders:
        if d and Path(d).is_dir():
            out += [p for p in Path(d).glob("*.zip") if "ffmpeg" in p.name.lower()]
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------------------
# winget — 없거나 정책으로 막혔으면 다음부터 건너뜀
# ---------------------------------------------------------------------------
WINGET_ARGS = ["install", "--id", "Gyan.FFmpeg", "-e", "--source", "winget", "--silent",
               "--disable-interactivity", "--accept-source-agreements",
               "--accept-package-agreements"]
_POLICY = re.compile(r"group polic|그룹 정책|disabled by .*polic|관리자가 .*차단", re.I)


@dataclass
class WingetOutcome:
    ffmpeg: str = ""
    reason: str = ""
    permanent: bool = False          # 다시 해도 같음 -> 다음 실행부터 건너뜀


def winget_install(*, which: Callable = shutil.which, run: Callable = subprocess.run,
                   find: Callable[[], str] = None,
                   say: Callable[[str], None] = None) -> WingetOutcome:
    say = say or _say
    find = find or (lambda: find_usable(say=lambda _m: None))
    exe = which("winget")
    if not exe:
        return WingetOutcome(reason="winget 이 없음(이 Windows 에는 앱 설치 관리자가 없음)",
                             permanent=True)
    say("winget 으로 ffmpeg 를 설치합니다... (약 250MB, 몇 분 걸립니다)")
    try:
        p = run([exe, *WINGET_ARGS], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return WingetOutcome(reason=f"winget 실행 실패: {exc}")
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines()[-8:]:
        if line.strip():
            say("      " + line.strip()[:160])
    # 종료 코드가 실패여도 이미 설치돼 있을 수 있어, 성공은 실제로 찾아서 판정
    found = find()
    if found:
        return WingetOutcome(ffmpeg=found)
    if _POLICY.search(out):
        return WingetOutcome(reason="winget 이 정책으로 막혀 있음", permanent=True)
    return WingetOutcome(reason=f"winget 설치 실패 (종료 코드 {p.returncode})")


# ---------------------------------------------------------------------------
# 원인별 자동 흐름: 찾기 -> 가져온 zip -> pip -> winget -> 릴리스 -> 다운로드 폴더 zip
# ---------------------------------------------------------------------------
@dataclass
class Step:
    name: str
    status: str                     # ok · fail · skip · fatal
    reason: str = ""


@dataclass
class AutoResult:
    ok: bool
    ffmpeg: str = ""
    steps: list[Step] = field(default_factory=list)
    next_action: str = ""
    sources: list[str] = field(default_factory=list)


def _state(path: Path | None) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path | None, data: dict) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass                                # 기록 실패로 설치를 멈추지 않음


def next_action(steps: list[Step], kinds: list[str], *, list_failed: bool = False,
                proxy: str | None = None) -> str:
    """전부 실패했을 때 사람이 할 일 하나."""
    reasons = " ".join(s.reason for s in steps)
    if "디스크" in reasons:
        return "디스크 공간을 1GB 이상 비운 뒤 다시 실행하세요."
    if "옮기지 못했습니다" in reasons:
        return "녹화·송출을 끈 뒤 다시 실행하세요(쓰던 ffmpeg 가 잠겨 있음)."
    net = kinds.count("net") + (1 if list_failed else 0)
    if net and net >= max(1, len(kinds) - kinds.count("hash") - kinds.count("feature")):
        if proxy:
            return f"프록시 설정(HTTPS_PROXY={proxy})이 맞는지 확인한 뒤 다시 실행하세요."
        return ("인터넷에 닿지 않습니다. 학교·회사망이면 HTTPS_PROXY 환경변수에 프록시 주소를 "
                "넣고 다시 실행하세요. 아니면 인터넷 연결을 확인하세요.")
    if kinds and all(k == "hash" for k in kinds):
        return ("받는 도중 파일이 바뀝니다(보안 장비 등). 다른 망에서 받거나 USB 로 가져오세요: "
                "zip 을 tools 폴더에 넣고 다시 실행.")
    return ("다른 PC 에서 https://github.com/GyanD/codexffmpeg/releases 의 "
            "*-essentials_build.zip 을 받아 USB 로 tools 폴더에 넣고 다시 실행하세요.")


def auto(dest: Path, *, state_path: Path | None = None, retry_skipped: bool = False,
         downloads: Path | None = None, allow_unverified: bool = False,
         find: Callable[[], str] = None, pip: Callable[[], str] = None,
         winget: Callable[[], WingetOutcome] = None, release: Callable[[], Result] = None,
         from_zip: Callable[[Path], str] = None,
         say: Callable[[str], None] = None) -> AutoResult:
    say = say or _say
    dest = Path(dest)
    find = find or (lambda: find_usable(say=say))
    pip = pip or (lambda: pip_install(say=say))
    winget = winget or (lambda: winget_install(say=say))
    release = release or (lambda: install(dest, say=say, allow_unverified=allow_unverified))
    from_zip = from_zip or (lambda z: install_from_zip(z, dest, say=say))
    r = AutoResult(False)
    state = _state(state_path)

    def done(name: str, exe: str) -> AutoResult:
        r.steps.append(Step(name, "ok"))
        r.ok, r.ffmpeg = True, exe
        say(f"쓸 수 있는 ffmpeg: {exe}  ({name})")
        return r

    exe = find()
    if exe:
        return done("이미 있음", exe)
    r.steps.append(Step("이미 있음", "fail", "쓸 수 있는 ffmpeg 없음"))

    for z in offline_zips(dest):                    # tools 에 가져다 둔 zip 이 먼저
        exe = from_zip(z)
        if exe:
            return done(f"가져온 zip ({z.name})", exe)
        r.steps.append(Step(f"가져온 zip ({z.name})", "fail", "zip 으로 설치 실패"))

    exe = pip()
    if exe:
        return done("pip (imageio-ffmpeg)", exe)
    r.steps.append(Step("pip (imageio-ffmpeg)", "fail", "pip 설치 실패 또는 기능 부족"))

    skip = (state.get("winget") or {}) if not retry_skipped else {}
    if skip.get("skip"):
        say(f"winget 은 건너뜁니다: {skip.get('reason')} (지난 실행에서 확인)")
        r.steps.append(Step("winget", "skip", str(skip.get("reason", ""))))
    else:
        w = winget()
        if w.ffmpeg:
            return done("winget", w.ffmpeg)
        r.steps.append(Step("winget", "fail", w.reason))
        if w.permanent:
            state["winget"] = {"skip": True, "reason": w.reason,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            _save_state(state_path, state)

    rel = release()
    r.sources = list(getattr(rel, "sources", []) or [])
    if rel.ok:
        return done(f"릴리스 목록 ({rel.used.version if rel.used else ''})", rel.ffmpeg)
    kinds = rel.kinds if len(rel.kinds) == len(rel.tried) else ["other"] * len(rel.tried)
    for (v, why), kind in zip(rel.tried, kinds, strict=True):
        r.steps.append(Step(f"릴리스 {v}", "fatal" if kind == "fatal" else "fail", why))
    if any(k == "fatal" for k in rel.kinds):
        r.next_action = next_action(r.steps, rel.kinds, list_failed=rel.list_failed)
        return r

    for z in offline_zips(downloads) if downloads else []:      # 마지막: 다운로드 폴더
        exe = from_zip(z)
        if exe:
            return done(f"다운로드 폴더 zip ({z.name})", exe)
        r.steps.append(Step(f"다운로드 폴더 zip ({z.name})", "fail", "zip 으로 설치 실패"))

    r.next_action = next_action(r.steps, rel.kinds, list_failed=rel.list_failed,
                                proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
    return r


def print_summary(r: AutoResult, say: Callable[[str], None] = None) -> None:
    say = say or _say
    if r.ok:
        return
    say("")
    if r.sources:
        say("[X] 릴리스 목록 출처별 결과:")
        for ln in r.sources:
            say(f"  - {ln}")
    say("[X] ffmpeg 를 설치하지 못했습니다. 시도한 순서와 이유:")
    for i, s in enumerate(r.steps, 1):
        mark = {"skip": "건너뜀", "fatal": "중단"}.get(s.status, "실패")
        say(f"  {i}. {s.name} — {mark}: {s.reason}")
    say(f"다음 할 일: {r.next_action}")


def find_usable(say: Callable[[str], None] = None) -> str:
    """0단계: 이미 쓸 수 있는 ffmpeg 가 있나(PATH·FFMPEG·tools·winget·pip). 없으면 ''."""
    say = say or _say
    from .recorder.ffmpeg_util import FFmpegMissing, find_ffmpeg

    try:
        exe = find_ffmpeg()
    except FFmpegMissing:
        return ""
    lack = missing_features(Path(exe))
    if lack:
        say(f"[!] 찾은 ffmpeg 에 녹화에 필요한 기능이 없습니다({', '.join(lack)}): {exe}\n"
            "    새로 설치해도 이것이 먼저 잡히면 쓰이지 않습니다. PATH 에서 빼거나\n"
            "    환경변수 FFMPEG 에 새 ffmpeg.exe 경로를 넣으세요.")
        return ""
    return exe


def pip_install(*, run: Callable = subprocess.run, say: Callable[[str], None] = None) -> str:
    """1단계: pip 의 imageio-ffmpeg(ffmpeg 실행 파일 동봉). 쓸 수 있으면 그 경로, 아니면 ''."""
    say = say or _say
    from .recorder import ffmpeg_util

    say("pip 로 imageio-ffmpeg 를 설치합니다... (약 40MB)")
    try:
        p = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                 "imageio-ffmpeg>=0.5"], timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"      [X] pip 를 실행하지 못했습니다: {exc}")
        return ""
    if p.returncode != 0:
        say(f"      [X] pip 설치 실패 (종료 코드 {p.returncode})")
        return ""
    ffmpeg_util.reset_cache()
    exe = ffmpeg_util.pip_ffmpeg()
    if not exe:
        say("      [X] 설치는 됐지만 쓸 수 있는 ffmpeg 가 없습니다(필요 기능 부족 등)")
    return exe or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m syncnote.ffmpeg_fetch",
                                 description="릴리스 목록에서 ffmpeg 를 받아 tools\\ffmpeg 에 둡니다")
    ap.add_argument("--find", action="store_true",
                    help="이미 쓸 수 있는 ffmpeg 가 있으면 경로를 출력하고 0 (설치 안 함)")
    ap.add_argument("--pip", action="store_true", help="pip 의 imageio-ffmpeg 로 설치")
    ap.add_argument("--auto", action="store_true",
                    help="찾기 -> 가져온 zip -> pip -> winget -> 릴리스 목록 -> 다운로드 폴더 zip")
    ap.add_argument("--retry-skipped", action="store_true",
                    help="지난번에 '다시 해도 같음' 으로 건너뛴 winget 도 다시 시도")
    ap.add_argument("--allow-unverified", action="store_true",
                    help="해시가 없는 후보(git 에서만 찾은 버전)도 시도 — 기본은 안 함")
    ap.add_argument("--from-zip", help="가져온 zip 으로 설치(오프라인)")
    ap.add_argument("--sha256", default="", help="--from-zip 의 SHA-256 (있으면 확인)")
    ap.add_argument("--dest", default=str(REPO_ROOT / "tools"))
    ap.add_argument("--max", type=int, default=6, help="롤백해 볼 릴리스 수 (기본 6)")
    ap.add_argument("--force", action="store_true", help="쓸 수 있는 ffmpeg 가 있어도 새로 받기")
    ap.add_argument("--dry-run", action="store_true", help="받지 않고 시도할 순서만 출력")
    a = ap.parse_args(argv)
    if a.from_zip:
        return 0 if install_from_zip(Path(a.from_zip), Path(a.dest), sha256=a.sha256) else 1
    if a.auto:
        downloads = Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"
        r = auto(Path(a.dest), state_path=Path(a.dest) / ".ffmpeg_install.json",
                 retry_skipped=a.retry_skipped, downloads=downloads,
                 allow_unverified=a.allow_unverified)
        print_summary(r)
        return 0 if r.ok else 1
    if a.find or a.pip:
        exe = find_usable() if a.find else pip_install()
        if exe:
            print(f"쓸 수 있는 ffmpeg: {exe}")
        return 0 if exe else 1
    if a.dry_run:
        from . import release_sources as RS

        sha = gyan_sha()
        cache_path = Path(a.dest) / CACHE_NAME
        cache = RS.load_cache(cache_path)
        entries, reports = RS.collect(api=fetch_releases, gyan_sha=lambda: sha,
                                      api_tag=fetch_release_tag, max_tries=a.max,
                                      allow_unverified=a.allow_unverified, cache=cache,
                                      save=lambda book: RS.save_cache(cache_path, book, cache))
        for rep in reports:
            print(f"출처: {rep.name} {'됨' if rep.ok else '안 됨'} — {rep.note}")
        cands = [to_candidate(e) for e in entries][:a.max] + [gyan_candidate(sha=sha)]
        for i, c in enumerate(cands, 1):
            mark = "·".join(c.verified_by) or "없음"
            made = " (만든 주소)" if c.synthesized else ""
            tier = f"T{min(3, 1 + len(c.verified_by))}" if c.sha256 else "T1"
            print(f"{i}. {c.version:<28} {tier} 출처 {'·'.join(c.sources):<14} 해시 {mark}{made}")
        return 0
    r = install(Path(a.dest), max_tries=a.max, force=a.force,
                allow_unverified=a.allow_unverified)
    if not r.ok and r.tried:
        print("\n시도한 순서와 실패 이유:")
        for v, why in r.tried:
            print(f"  - {v}: {why}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    sys.exit(main())
