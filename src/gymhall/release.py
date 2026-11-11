"""GitHub 릴리스 업로더.

왜 이렇게 복잡한가:
  - 릴리스 에셋 1개의 상한이 2GiB 입니다. 1시간짜리 화면녹화는 이걸 넘길 수 있어
    자동 분할 + 복원 안내가 필요합니다.
  - 에셋 이름은 중복될 수 없고, GitHub 가 일부 문자를 마음대로 바꿉니다.
    (공백 -> '.', 비ASCII -> 제거) 한글 파일명이 깨지므로 안전한 이름으로
    바꿔 올리고, 원래 이름은 manifest 에 남깁니다.
  - 업로드 중 끊기면 '절반 올라간 에셋' 이 남습니다. 먼저 지우고 다시 올립니다.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import secrets
from .logging_setup import get as get_logger
from .state import sha256_file

log = get_logger("release")

API = "https://api.github.com"
GITHUB_ASSET_LIMIT = 2 * 1024**3  # 2 GiB, GitHub 하드 리밋


class ReleaseError(Exception):
    pass


# ---------------------------------------------------------------------------
# 에셋 이름
# ---------------------------------------------------------------------------
_ASSET_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def asset_name(original: str, prefix: str = "") -> str:
    """GitHub 가 변형하지 않는 이름으로 바꿉니다.

    GitHub 는 에셋 이름의 공백을 '.' 으로 바꾸고 비ASCII 문자를 버립니다.
    한글 제목은 그대로 쓸 수 없으므로 안전한 글자만 남기는데, 그러면
    "운동과건강 1주차.pptx" 가 "1.pptx" 가 되어 다른 과목 파일과 충돌합니다.
    그래서 원래 이름에 ASCII 가 아닌 글자가 있었다면 원본 이름의 해시
    8자리를 붙여 유일성을 보장합니다. 사람이 읽을 원래 이름은 에셋의
    label 과 분할 manifest 에 그대로 남습니다.
    """
    name = unicodedata.normalize("NFC", original)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    safe_stem = _ASSET_SAFE.sub("_", stem).strip("_")
    safe_ext = _ASSET_SAFE.sub("", ext)[:12]
    safe_prefix = _ASSET_SAFE.sub("_", unicodedata.normalize("NFC", prefix)).strip("_")
    if safe_prefix:
        safe_prefix += "_"

    # 원본에 비ASCII 가 섞여 있었다면 정보가 날아간 것이므로 해시로 보강합니다.
    lost = any(ord(ch) > 127 for ch in stem)
    if lost or not safe_stem:
        digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:8]
        safe_stem = f"{safe_stem}_{digest}".strip("_") if safe_stem else digest

    out = f"{safe_prefix}{safe_stem}"
    if safe_ext:
        out = f"{out}.{safe_ext}"
    return out[:200]


# ---------------------------------------------------------------------------
# 태그 전략
# ---------------------------------------------------------------------------
def tag_for(strategy: str, prefix: str, when: date | None = None, fixed: str = "archive") -> str:
    when = when or date.today()
    if strategy == "single":
        return f"{prefix}-{fixed}" if prefix else fixed
    if strategy == "daily":
        suffix = when.strftime("%Y-%m-%d")
    elif strategy == "weekly":
        iso = when.isocalendar()
        suffix = f"{iso[0]}-W{iso[1]:02d}"
    else:  # monthly
        suffix = when.strftime("%Y-%m")
    return f"{prefix}-{suffix}" if prefix else suffix


# ---------------------------------------------------------------------------
# 분할
# ---------------------------------------------------------------------------
@dataclass
class Part:
    path: Path
    index: int
    total: int


def split_file(src: Path, out_dir: Path, chunk_bytes: int) -> list[Part]:
    """src 를 chunk_bytes 크기로 쪼개 out_dir 에 .partNNN 파일로 씁니다."""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    size = src.stat().st_size
    total = max(1, -(-size // chunk_bytes))  # ceil
    parts: list[Part] = []
    with src.open("rb") as fh:
        for idx in range(1, total + 1):
            part_path = out_dir / f"{src.name}.part{idx:03d}"
            written = 0
            with part_path.open("wb") as out:
                while written < chunk_bytes:
                    block = fh.read(min(8 * 1024 * 1024, chunk_bytes - written))
                    if not block:
                        break
                    out.write(block)
                    written += len(block)
            parts.append(Part(part_path, idx, total))
    log.info("분할: %s -> %d 조각", src.name, total)
    return parts


RESTORE_SH = """#!/usr/bin/env bash
# 조각 파일을 원본으로 합칩니다.  사용법:  bash restore.sh <원본이름>
set -e
name="$1"
cat "${name}".part* > "${name}"
echo "복원 완료: ${name}"
"""

RESTORE_BAT = """@echo off
REM 조각 파일을 원본으로 합칩니다.  사용법:  restore.bat 원본이름
if "%~1"=="" (echo 사용법: restore.bat 원본파일이름 & exit /b 1)
copy /b "%~1.part*" "%~1"
echo 복원 완료: %~1
"""


# ---------------------------------------------------------------------------
# API 클라이언트
# ---------------------------------------------------------------------------
class ReleaseUploader:
    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        *,
        retry_count: int = 4,
        retry_backoff: float = 2.0,
        split_size_mb: int = 1900,
        draft: bool = False,
        prerelease: bool = False,
        dry_run: bool = False,
    ) -> None:
        if not owner or not repo:
            raise ReleaseError("github.owner / github.repo 가 설정되지 않았습니다.")
        self.owner = owner
        self.repo = repo
        self.retry_count = max(0, int(retry_count))
        self.retry_backoff = float(retry_backoff)
        self.chunk_bytes = min(int(split_size_mb) * 1024 * 1024, GITHUB_ASSET_LIMIT - 1)
        self.draft = draft
        self.prerelease = prerelease
        self.dry_run = dry_run
        self._token = token
        self._session = None
        self._release_cache: dict[str, dict[str, Any]] = {}

    # -- 하부 ---------------------------------------------------------
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = secrets.get("github") or ""
        if not self._token:
            raise ReleaseError("GitHub 토큰이 없습니다. python -m gymhall secrets set github")
        return self._token

    @property
    def session(self):
        if self._session is None:
            try:
                import requests  # type: ignore
            except ImportError as exc:
                raise ReleaseError("requests 패키지가 필요합니다: pip install requests") from exc
            s = requests.Session()
            s.headers.update(
                {
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "gym-w-hall-2f",
                }
            )
            self._session = s
        return self._session

    def _request(self, method: str, url: str, **kwargs) -> Any:
        """재시도 포함 요청. 네트워크 오류와 5xx 만 재시도합니다."""
        import requests  # type: ignore

        last_exc: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                resp = self.session.request(method, url, timeout=kwargs.pop("timeout", 120), **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.retry_count:
                    break
                wait = self.retry_backoff ** (attempt + 1)
                log.warning("네트워크 오류(%s). %.0f초 후 재시도 %d/%d",
                            exc.__class__.__name__, wait, attempt + 1, self.retry_count)
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                raise ReleaseError("GitHub 토큰이 거부됐습니다(401). 토큰이 만료됐거나 권한이 없습니다.")
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = resp.headers.get("X-RateLimit-Reset")
                raise ReleaseError(f"GitHub API 호출 한도 초과. 재설정 시각(epoch): {reset}")
            if resp.status_code == 404 and method == "GET":
                return None
            if 500 <= resp.status_code < 600 and attempt < self.retry_count:
                wait = self.retry_backoff ** (attempt + 1)
                log.warning("GitHub %d. %.0f초 후 재시도 %d/%d",
                            resp.status_code, wait, attempt + 1, self.retry_count)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise ReleaseError(f"GitHub {method} {url} -> {resp.status_code}: {resp.text[:400]}")
            if not resp.content:
                return {}
            try:
                return resp.json()
            except json.JSONDecodeError:
                return {}
        raise ReleaseError(f"재시도 {self.retry_count}회 모두 실패: {last_exc}")

    # -- 릴리스 -------------------------------------------------------
    def get_or_create_release(self, tag: str, *, name: str | None = None,
                              body: str | None = None) -> dict[str, Any]:
        if tag in self._release_cache:
            return self._release_cache[tag]

        # dry-run 은 네트워크를 전혀 건드리지 않습니다.
        # 토큰이 없어도, 저장소가 아직 없어도 무엇이 올라갈지 볼 수 있어야 합니다.
        if self.dry_run:
            fake = {"id": 0, "tag_name": tag, "assets": [], "upload_url": "", "html_url": "(dry-run)"}
            self._release_cache[tag] = fake
            log.info("[dry-run] 릴리스 '%s' 를 쓰는 것으로 가정합니다.", tag)
            return fake

        existing = self._request("GET", f"{API}/repos/{self.owner}/{self.repo}/releases/tags/{tag}")
        if existing:
            self._release_cache[tag] = existing
            return existing

        log.info("릴리스 생성: %s", tag)
        created = self._request(
            "POST",
            f"{API}/repos/{self.owner}/{self.repo}/releases",
            json={
                "tag_name": tag,
                "name": name or tag,
                "body": body or _default_body(tag),
                "draft": self.draft,
                "prerelease": self.prerelease,
            },
        )
        self._release_cache[tag] = created
        return created

    def list_assets(self, release: dict[str, Any]) -> list[dict[str, Any]]:
        rid = release.get("id")
        if not rid or self.dry_run:
            return []
        assets = self._request(
            "GET", f"{API}/repos/{self.owner}/{self.repo}/releases/{rid}/assets?per_page=100"
        )
        return assets or []

    def delete_asset(self, asset_id: int) -> None:
        if self.dry_run:
            return
        self._request("DELETE", f"{API}/repos/{self.owner}/{self.repo}/releases/assets/{asset_id}")

    # -- 업로드 -------------------------------------------------------
    def upload_one(self, release: dict[str, Any], path: Path, *,
                   name: str | None = None, label: str | None = None) -> dict[str, Any]:
        """파일 하나를 에셋으로 올립니다. 같은 이름이 있으면 지우고 다시 올립니다."""
        path = Path(path)
        target = name or asset_name(path.name)

        if self.dry_run:
            log.info("[dry-run] 업로드 생략: %s -> %s (%s)", path.name, target, _human(path.stat().st_size))
            return {"name": target, "browser_download_url": "(dry-run)"}

        for asset in self.list_assets(release):
            if asset.get("name") == target:
                log.info("같은 이름의 에셋을 교체합니다: %s", target)
                self.delete_asset(asset["id"])
                break

        upload_url = str(release.get("upload_url", "")).split("{")[0]
        if not upload_url:
            raise ReleaseError("릴리스에 upload_url 이 없습니다.")

        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        log.info("업로드 시작: %s (%s)", target, _human(size))

        params = {"name": target}
        if label:
            params["label"] = label

        with path.open("rb") as fh:
            result = self._request(
                "POST",
                upload_url,
                params=params,
                data=fh,
                headers={"Content-Type": ctype, "Content-Length": str(size)},
                timeout=None,
            )
        log.info("업로드 완료: %s", target)
        return result

    def upload(self, path: Path, tag: str, *, prefix: str = "",
               keep_parts: bool = False, tmp_dir: Path | None = None) -> dict[str, Any]:
        """파일 하나를 릴리스에 올립니다. 2GiB 를 넘으면 자동 분할합니다.

        반환: {"tag", "assets": [...], "split": bool, "sha256": str}
        """
        path = Path(path)
        if not path.exists():
            raise ReleaseError(f"올릴 파일이 없습니다: {path}")

        size = path.stat().st_size
        digest = sha256_file(path)
        release = self.get_or_create_release(tag)
        base = asset_name(path.name, prefix=prefix)

        if size <= self.chunk_bytes:
            asset = self.upload_one(release, path, name=base, label=path.name)
            return {"tag": tag, "assets": [asset], "split": False, "sha256": digest}

        # --- 분할 ---
        log.warning(
            "%s 가 %s 로 상한(%s)을 넘습니다. 분할 업로드합니다.",
            path.name, _human(size), _human(self.chunk_bytes),
        )
        work = Path(tmp_dir or path.parent) / f".parts-{path.stem}"
        parts = split_file(path, work, self.chunk_bytes)

        manifest = {
            "original_name": path.name,
            "original_size": size,
            "sha256": digest,
            "part_count": len(parts),
            "part_prefix": base,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "restore": {
                "windows": f'copy /b "{base}.part*" "{path.name}"',
                "linux_macos": f'cat {base}.part* > "{path.name}"',
            },
        }
        manifest_path = work / f"{path.name}.parts.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (work / "restore.sh").write_text(RESTORE_SH, encoding="utf-8", newline="\n")
        (work / "restore.bat").write_text(RESTORE_BAT, encoding="utf-8", newline="\r\n")

        uploaded = []
        try:
            for part in parts:
                uploaded.append(
                    self.upload_one(
                        release, part.path,
                        name=f"{base}.part{part.index:03d}",
                        label=f"{path.name} ({part.index}/{part.total})",
                    )
                )
            uploaded.append(
                self.upload_one(release, manifest_path, name=f"{base}.parts.json",
                                label=f"{path.name} 복원 정보")
            )
        finally:
            if not keep_parts:
                for part in parts:
                    part.path.unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
                (work / "restore.sh").unlink(missing_ok=True)
                (work / "restore.bat").unlink(missing_ok=True)
                try:
                    work.rmdir()
                except OSError:
                    pass

        return {"tag": tag, "assets": uploaded, "split": True, "sha256": digest}


# ---------------------------------------------------------------------------
def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TiB"


def _default_body(tag: str) -> str:
    return (
        f"`{tag}` 자동 생성 릴리스입니다.\n\n"
        "이 릴리스의 에셋은 자동으로 누적됩니다.\n\n"
        "**2GiB 를 넘는 파일은 `.partNNN` 으로 분할되어 올라갑니다.**\n"
        "같이 올라온 `*.parts.json` 에 원본 이름과 SHA-256 이 들어 있고,\n"
        "복원은 아래와 같이 합니다.\n\n"
        "```bat\ncopy /b \"이름.part*\" \"원본이름.mkv\"\n```\n"
        "```bash\ncat 이름.part* > 원본이름.mkv\n```\n"
    )


def upload_many(
    uploader: ReleaseUploader,
    files: list[Path],
    tag: str,
    *,
    prefix: str = "",
    store=None,
    on_done: Callable[[Path, dict], None] | None = None,
) -> list[dict[str, Any]]:
    """여러 파일을 올리되 이미 올린 것(해시 동일)은 건너뜁니다."""
    results = []
    for path in files:
        path = Path(path)
        if not path.exists():
            log.warning("건너뜀(파일 없음): %s", path)
            continue
        digest = sha256_file(path)
        if store is not None and store.is_uploaded(digest):
            log.info("건너뜀(이미 업로드됨): %s", path.name)
            continue
        try:
            result = uploader.upload(path, tag, prefix=prefix)
        except ReleaseError as exc:
            log.error("업로드 실패: %s -> %s", path.name, exc)
            continue
        if store is not None:
            store.mark_uploaded(
                digest, tag=tag,
                asset_name=result["assets"][0].get("name", "") if result["assets"] else "",
                size=path.stat().st_size,
            )
            store.save()
        if on_done:
            on_done(path, result)
        results.append(result)
    return results


def iter_files(root: Path, exclude_dirs: set[str] | None = None) -> Iterator[Path]:
    exclude_dirs = exclude_dirs or set()
    for p in sorted(Path(root).rglob("*")):
        if p.is_dir():
            continue
        if any(part in exclude_dirs for part in p.parts):
            continue
        if p.name.startswith("."):
            continue
        yield p
