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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

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


def asset_name(original: str, prefix: str = "", unique_key: str | None = None) -> str:
    """GitHub 가 변형하지 않는 이름으로 바꿉니다.

    GitHub 는 에셋 이름의 공백을 '.' 으로 바꾸고 비ASCII 문자를 버립니다.
    한글 제목은 그대로 쓸 수 없으므로 안전한 글자만 남기는데, 그러면
    "운동과건강 1주차.pptx" 가 "1.pptx" 가 되어 다른 과목 파일과 충돌합니다.
    그래서 한글이 섞여 있으면 해시 8자리를 붙입니다.

    **unique_key 를 반드시 넘기세요.** 기본값(파일 이름)으로 두면 과목이
    달라도 같은 이름은 같은 해시가 됩니다. 2026-11-12 실제 실행에서
    9개 과목의 '강의목록.md' 가 전부 809b1351.md 가 되어 서로를
    덮어썼습니다. 16개를 올렸는데 릴리스에는 2개만 남았고, 로그에는
    '업로드 완료' 가 16번 찍혔습니다. unique_key 에는 학기 폴더 기준
    상대 경로를 넣습니다.
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
    key = unique_key if unique_key is not None else stem
    if lost or not safe_stem:
        digest = hashlib.sha1(unicodedata.normalize("NFC", key).encode("utf-8")).hexdigest()[:8]
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
        self._asset_cache: dict[int, list[dict[str, Any]]] = {}

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

    def _request(self, method: str, url: str, *,
                 data_factory: Callable[[], Any] | None = None, **kwargs) -> Any:
        """재시도 포함 요청.

        data_factory 를 주면 '시도마다' 새로 호출해 본문을 만듭니다.
        파일 객체를 data= 로 한 번만 넘기면, 첫 시도에서 핸들이 끝까지
        읽힌 뒤 재시도에서 0바이트를 보내게 됩니다. 그러면 GitHub 는
        빈 에셋을 받아들이고 200 을 돌려주므로, 녹화본이 백업된 줄
        알았는데 0바이트인 상황이 조용히 만들어집니다.
        """
        import requests  # type: ignore

        timeout = kwargs.pop("timeout", 120)
        last_exc: Exception | None = None
        for attempt in range(self.retry_count + 1):
            body = None
            try:
                if data_factory is not None:
                    body = data_factory()
                    kwargs["data"] = body
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                _close(body)
                last_exc = exc
                if attempt >= self.retry_count:
                    break
                wait = self.retry_backoff ** (attempt + 1)
                log.warning("네트워크 오류(%s). %.0f초 후 재시도 %d/%d",
                            exc.__class__.__name__, wait, attempt + 1, self.retry_count)
                time.sleep(wait)
                continue
            else:
                _close(body)

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

    def list_assets(self, release: dict[str, Any], *, refresh: bool = False) -> list[dict[str, Any]]:
        """릴리스의 에셋 목록. 한 번 받아 캐시합니다.

        파일 하나당 한 번씩 조회하면 수백 개를 올릴 때 API 호출이 두 배가
        됩니다. 업로드/삭제할 때 캐시를 직접 갱신해 일관성을 유지합니다.
        """
        rid = release.get("id")
        if not rid or self.dry_run:
            return []
        if not refresh and rid in self._asset_cache:
            return self._asset_cache[rid]

        assets: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request(
                "GET",
                f"{API}/repos/{self.owner}/{self.repo}/releases/{rid}/assets",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            if not isinstance(batch, list):
                # 에셋 목록은 반드시 배열입니다. 아니면 API 응답이 예상과
                # 다른 것이므로, 문자열을 에셋으로 오인해 망가지기 전에 멈춥니다.
                log.warning("에셋 목록 응답이 배열이 아닙니다(%s). 목록을 비워 둡니다.", type(batch).__name__)
                assets = []
                break
            assets.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < 100:
                break
            page += 1
            if page > 50:  # 5000개면 뭔가 잘못된 것입니다
                log.warning("에셋이 5000개를 넘습니다. 목록 조회를 멈춥니다.")
                break

        self._asset_cache[rid] = assets
        return assets

    def _remember_asset(self, release: dict[str, Any], asset: dict[str, Any]) -> None:
        rid = release.get("id")
        if rid in self._asset_cache:
            self._asset_cache[rid].append(asset)

    def _forget_asset(self, release: dict[str, Any], name: str) -> None:
        rid = release.get("id")
        if rid in self._asset_cache:
            self._asset_cache[rid] = [
                a for a in self._asset_cache[rid] if a.get("name") != name
            ]

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
                self._forget_asset(release, target)
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

        result = self._request(
            "POST",
            upload_url,
            params=params,
            # 재시도마다 파일을 다시 엽니다. 한 번만 열어서 넘기면
            # 재시도 때 0바이트가 올라갑니다.
            data_factory=lambda: path.open("rb"),
            headers={"Content-Type": ctype, "Content-Length": str(size)},
            timeout=None,
        )

        # GitHub 가 돌려준 크기를 로컬 파일과 대조합니다.
        # 이게 없으면 0바이트 에셋이 성공으로 보고돼도 알 수 없습니다.
        reported = result.get("size") if isinstance(result, dict) else None
        if isinstance(reported, int) and reported != size:
            raise ReleaseError(
                f"업로드된 크기가 다릅니다: {target} — "
                f"보낸 {size}바이트 / 저장된 {reported}바이트. "
                "업로드가 중간에 끊겼을 수 있으니 다시 시도하세요."
            )

        if isinstance(result, dict):
            self._remember_asset(release, result)
        log.info("업로드 완료: %s", target)
        return result

    def upload(self, path: Path, tag: str, *, prefix: str = "",
               keep_parts: bool = False, tmp_dir: Path | None = None,
               rel_path: str | None = None) -> dict[str, Any]:
        """파일 하나를 릴리스에 올립니다. 2GiB 를 넘으면 자동 분할합니다.

        반환: {"tag", "assets": [...], "split": bool, "sha256": str}
        """
        path = Path(path)
        if not path.exists():
            raise ReleaseError(f"올릴 파일이 없습니다: {path}")

        size = path.stat().st_size
        digest = sha256_file(path)
        release = self.get_or_create_release(tag)
        # 이름이 같은 파일이 과목마다 있습니다(강의목록.md 등). 상대 경로로
        # 구분하지 않으면 서로 덮어씁니다.
        base = asset_name(path.name, prefix=prefix, unique_key=rel_path or path.name)

        if size <= self.chunk_bytes:
            # label 에는 어느 과목의 무엇인지 보이도록 상대 경로를 넣습니다.
            # 에셋 이름은 해시라서 label 이 유일한 단서입니다.
            asset = self.upload_one(release, path, name=base, label=rel_path or path.name)
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
def _close(obj: Any) -> None:
    """파일 객체면 닫습니다. 그 외에는 아무것도 하지 않습니다."""
    closer = getattr(obj, "close", None)
    if callable(closer):
        try:
            closer()
        except OSError:
            pass


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
    root: Path | None = None,
    on_done: Callable[[Path, dict], None] | None = None,
) -> list[dict[str, Any]]:
    """여러 파일을 올리되 이미 올린 것(해시 동일)은 건너뜁니다.

    root 를 주면 에셋 이름을 그 기준 상대 경로로 만듭니다. 주지 않으면
    파일 이름만 쓰므로, 과목마다 있는 같은 이름 파일이 서로 덮어씁니다.
    """
    results = []
    # 한 번의 실행 안에서 같은 에셋 이름이 두 번 나오면 반드시 버그입니다
    # (두 번째가 첫 번째를 지웁니다). 조용히 넘어가면 사용자는 전부
    # 올라간 줄 압니다 — 2026-11-12 실행에서 16개 중 14개가 이렇게
    # 사라졌고 로그에는 '업로드 완료' 가 16번 찍혔습니다.
    taken: dict[str, Path] = {}
    for path in files:
        path = Path(path)
        if not path.exists():
            log.warning("건너뜀(파일 없음): %s", path)
            continue
        rel = None
        if root is not None:
            try:
                rel = str(Path(path).relative_to(root))
            except ValueError:
                rel = path.name
        target = asset_name(path.name, prefix=prefix, unique_key=rel or path.name)
        if target in taken:
            log.error(
                "에셋 이름이 겹칩니다: %s <- %s / %s. 덮어쓰지 않고 건너뜁니다.",
                target, taken[target], path,
            )
            continue
        taken[target] = path

        digest = sha256_file(path)
        if store is not None and store.is_uploaded(digest):
            log.info("건너뜀(이미 업로드됨): %s", rel or path.name)
            continue
        try:
            result = uploader.upload(path, tag, prefix=prefix, rel_path=rel)
        except ReleaseError as exc:
            log.error("업로드 실패: %s -> %s", rel or path.name, exc)
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


def upload_filter(
    files: Iterable[Path],
    *,
    allow_extensions: Iterable[str] = (),
    max_file_mb: int = 0,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """업로드할 것과 뺄 것을 가릅니다. (올릴 목록, [(파일, 이유)])

    설정의 eclass.upload.allow_extensions / max_file_mb 가 여기서
    처음으로 실제 효과를 냅니다. 전에는 수집기가 두 값을 받아 로그만
    찍고 결과를 버려서, 화이트리스트를 적어도 전부 올라갔고 용량
    제한도 지켜지지 않았습니다.

    뺀 파일은 지우지 않습니다. 로컬에는 남고 릴리스에만 안 올라갑니다.
    """
    allow = {str(e).lower().lstrip(".") for e in allow_extensions if str(e).strip()}
    limit = int(max_file_mb) * 1024 * 1024 if max_file_mb else 0

    keep: list[Path] = []
    dropped: list[tuple[Path, str]] = []
    for path in files:
        ext = path.suffix.lower().lstrip(".")
        if allow and ext not in allow:
            dropped.append((path, f"확장자 '{ext or '없음'}' 가 허용 목록에 없음"))
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            dropped.append((path, f"크기를 읽지 못함: {exc}"))
            continue
        if limit and size > limit:
            dropped.append((path, f"{size / 1024 / 1024:.1f}MB > 제한 {max_file_mb}MB"))
            continue
        keep.append(path)
    return keep, dropped


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
