"""무엇을 이미 받았고 이미 올렸는지 기억합니다.

'생길 때마다 다운로드' 가 성립하려면 '이미 있는 것'을 알아야 합니다.
파일 내용 해시로 판단하므로, 교수가 같은 이름으로 파일을 교체하면
새 파일로 인식해 다시 받습니다. 반대로 이름만 바뀌고 내용이 같으면
중복 업로드하지 않습니다.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: str) -> None:
    """중간에 전원이 꺼져도 상태 파일이 깨지지 않도록 임시파일 + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).replace(path)
    except BaseException:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class Store:
    """JSON 한 파일로 된 작은 기록장."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    # 이번 실행에서 지운 키. 병합이 되살리지 못하게 합니다.
    _tombstones: dict[str, set[str]] = field(default_factory=dict, repr=False)

    # -- 수명주기 ----------------------------------------------------
    @classmethod
    def open(cls, path: Path) -> Store:
        path = Path(path)
        data: dict[str, Any] = {"version": 1, "downloaded": {}, "uploaded": {}, "meta": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except (json.JSONDecodeError, OSError):
                # 깨진 상태 파일은 백업하고 새로 시작합니다.
                backup = path.with_suffix(f".corrupt-{datetime.now():%Y%m%d%H%M%S}.json")
                try:
                    path.replace(backup)
                except OSError:
                    pass
        data.setdefault("downloaded", {})
        data.setdefault("uploaded", {})
        data.setdefault("meta", {})
        return cls(path=path, data=data)

    def save(self) -> None:
        """디스크에 씁니다. **다른 프로세스가 쓴 것을 지우지 않습니다.**

        _LOCK 은 스레드 락이라 프로세스 사이를 막지 못합니다.
        RecordSchedule 이 세그먼트를 올리면서 recordings.json 을 쓰고,
        같은 시각 TranscribeQueue 가 전사 결과를 올리면서 같은 파일을
        쓰면, _atomic_write 는 파일 단위 원자성만 보장합니다. 둘이 각자
        읽고 각자 쓰면 **늦게 쓴 쪽이 이기고 앞엣것은 사라집니다.**

        그래서 쓰기 직전에 디스크를 다시 읽어 합칩니다. 기록은 키 단위로
        더해지기만 하므로(지우는 일이 없음) 합집합이면 충분합니다.
        내가 들고 있는 값이 이깁니다 — 방금 바꾼 쪽이 최신입니다.
        """
        with _LOCK:
            merged = self._merged_with_disk()
            self.data = merged
            _atomic_write(self.path, json.dumps(merged, ensure_ascii=False, indent=1))

    # 합칠 대상. 키 -> 값 꼴의 기록장들입니다.
    _MERGE_SECTIONS = ("downloaded", "uploaded", "meta")

    def forget(self, section: str, key: str) -> None:
        """기록 하나를 지웁니다.

        그냥 dict 에서 pop 하면 안 됩니다 — save 가 디스크와 합치면서
        그 키를 **되살립니다.** 지웠다는 사실을 따로 들고 있다가 합칠
        때 빼야 합니다.
        """
        self.data.get(section, {}).pop(key, None)
        self._tombstones.setdefault(section, set()).add(key)

    def _merged_with_disk(self) -> dict[str, Any]:
        try:
            disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.data
        if not isinstance(disk, dict):
            return self.data

        out = dict(disk)
        out.update({k: v for k, v in self.data.items()
                    if k not in self._MERGE_SECTIONS})
        for section in self._MERGE_SECTIONS:
            a = disk.get(section)
            b = self.data.get(section)
            if not isinstance(a, dict):
                a = {}
            if not isinstance(b, dict):
                b = {}
            merged = {**a, **b}            # 내가 들고 있는 쪽이 이깁니다
            for gone in self._tombstones.get(section, ()):
                merged.pop(gone, None)     # 내가 지운 것은 되살리지 않습니다
            out[section] = merged
        return out

    # -- 다운로드 기록 ------------------------------------------------
    def is_downloaded(self, key: str) -> bool:
        return key in self.data["downloaded"]

    def mark_downloaded(self, key: str, *, path: str | None = None, digest: str | None = None,
                        extra: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if path:
            entry["path"] = str(path)
        if digest:
            entry["sha256"] = digest
        if extra:
            entry.update(extra)
        self.data["downloaded"][key] = entry

    def downloaded_digests(self) -> set[str]:
        return {
            e["sha256"]
            for e in self.data["downloaded"].values()
            if isinstance(e, dict) and e.get("sha256")
        }

    # -- 업로드 기록 --------------------------------------------------
    @staticmethod
    def _upload_key(digest: str, repo: str = "") -> str:
        """레포가 여러 개면 '같은 내용' 이라도 레포마다 따로 올라갑니다.

        digest 만으로 기억하면, A레포에 올린 파일과 내용이 같은 것이
        B레포에서 '이미 올렸다' 로 걸려 조용히 빠집니다. 과목 = 레포
        구조에서는 같은 강의자료가 두 과목에 다 올라가는 것이 정상입니다.
        """
        return f"{repo}|{digest}" if repo else digest

    def is_uploaded(self, digest: str, repo: str = "") -> bool:
        return self._upload_key(digest, repo) in self.data["uploaded"]

    def mark_uploaded(self, digest: str, *, tag: str, asset_name: str, repo: str = "",
                      size: int | None = None) -> None:
        self.data["uploaded"][self._upload_key(digest, repo)] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tag": tag,
            "repo": repo,
            "asset": asset_name,
            "size": size,
        }

    # -- 임의 메타 ----------------------------------------------------
    def get_meta(self, key: str, default: Any = None) -> Any:
        return self.data["meta"].get(key, default)

    def set_meta(self, key: str, value: Any) -> None:
        self.data["meta"][key] = value

    # -- 정보 --------------------------------------------------------
    def summary(self) -> str:
        return (
            f"다운로드 기록 {len(self.data['downloaded'])}건, "
            f"업로드 기록 {len(self.data['uploaded'])}건"
        )
