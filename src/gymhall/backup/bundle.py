"""홈 파일을 zip 묶음으로 만들기.

release.make_archive 를 쓰지 않는 이유: 파일을 통째로 메모리에 읽습니다.
홈에는 수 GB 짜리 파일이 있습니다. 여기서는 1MiB 씩 흘려 쓰면서
**써 넣은 바이트 그대로** sha256 을 계산합니다. 읽는 도중 파일이
바뀌어도 매니페스트의 해시는 실제로 올라간 내용과 맞습니다.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("backup")

CHUNK = 1024 * 1024
# 이미 압축된 형식은 다시 눌러 봐야 CPU 만 씁니다.
STORED_EXT = {".mp4", ".zip", ".jpg", ".jpeg", ".png", ".mkv", ".mp3", ".7z", ".rar", ".gz"}


@dataclass(frozen=True)
class Packed:
    """묶음에 들어간 파일 하나."""

    path: Path
    member: str      # zip 안 이름 = 홈 기준 상대 POSIX 경로
    size: int        # 실제로 써 넣은 바이트 수
    mtime_ns: int    # 스캔 때 본 값
    sha256: str      # 실제로 써 넣은 바이트의 해시


@dataclass
class BundleOut:
    packed: list[Packed] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)


def plan_bundles(files: Iterable[tuple[Path, int, int]], limit_bytes: int, *,
                 max_files: int = 0) -> Iterator[list[tuple[Path, int, int]]]:
    """원본 합계가 limit_bytes 를 넘지 않게 나눠 **하나씩** 내놓습니다.

    입력도 이터레이터로 받습니다. 메모리에는 묶음 하나만 있습니다.
    한 파일이 limit_bytes 보다 크면 그 파일 혼자 한 묶음입니다(업로더가 2GiB 분할).
    max_files 를 주면 묶음 하나의 파일 수도 그 이하로 자릅니다(사슬 고리 상한).
    """
    limit = max(1, int(limit_bytes))
    cur: list[tuple[Path, int, int]] = []
    total = 0
    for item in files:
        size = item[1]
        # 큰 파일은 이 조건 하나로 혼자가 됩니다: 앞 묶음을 닫고 들어가고,
        # 다음 파일이 오면 이미 한도를 넘었으므로 다시 닫힙니다.
        if cur and (total + size > limit or (max_files and len(cur) >= max_files)):
            yield cur
            cur, total = [], 0
        cur.append(item)
        total += size
    if cur:
        yield cur


def _zip_time(mtime_ns: int) -> tuple[int, int, int, int, int, int]:
    try:
        t = time.localtime(mtime_ns / 1e9)[:6]
    except (OverflowError, OSError, ValueError):
        return (1980, 1, 1, 0, 0, 0)
    if t[0] < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if t[0] > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return t  # type: ignore[return-value]


class _ReadFailed(Exception):
    """zip 항목을 연 뒤에 읽기가 실패했습니다. 그 항목은 반쯤 써졌습니다."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.reason = reason


def _write(home: Path, files: list[tuple[Path, int, int]], dest: Path,
           skip: set[Path], out: BundleOut) -> None:
    with zipfile.ZipFile(dest, "w", allowZip64=True) as zf:
        # ZipFile.open 은 텍스트 모드가 없습니다(항목 바이트 쓰기 전용).
        member_writer = zf.open
        for path, _size, mtime_ns in files:
            if path in skip:
                continue
            member = path.relative_to(home).as_posix()
            try:
                src = path.open("rb")
            except OSError as exc:
                # 아직 zip 에 아무것도 안 썼으므로 깔끔하게 뺄 수 있습니다.
                out.unreadable.append((path, f"열 수 없음: {exc.__class__.__name__}"))
                skip.add(path)
                continue
            info = zipfile.ZipInfo(member, date_time=_zip_time(mtime_ns))
            stored = path.suffix.lower() in STORED_EXT
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            if not stored:
                # 3.10 에는 compress_level 속성이 없습니다(3.13 부터 이 칸의 별칭).
                info._compresslevel = 6  # type: ignore[attr-defined]
            info.external_attr = 0o644 << 16
            h = hashlib.sha256()
            written = 0
            with src, member_writer(info, "w", force_zip64=True) as dst:
                while True:
                    try:
                        block = src.read(CHUNK)
                    except OSError as exc:
                        raise _ReadFailed(path, f"읽는 중 오류: {exc.__class__.__name__}") from exc
                    if not block:
                        break
                    dst.write(block)
                    h.update(block)
                    written += len(block)
            out.packed.append(Packed(path, member, written, int(mtime_ns), h.hexdigest()))


def write_bundle(home: Path, files: list[tuple[Path, int, int]], dest: Path) -> BundleOut:
    """files 를 dest zip 으로 씁니다. 못 읽은 파일은 빼고 unreadable 에 담습니다.

    읽기가 도중에 실패하면 그 항목은 zip 안에 반쯤 남습니다. zip 에서
    항목을 지울 수는 없으므로 그 파일을 빼고 묶음을 처음부터 다시 씁니다.
    """
    home = Path(home)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    skip: set[Path] = set()
    out = BundleOut()
    while True:
        out.packed = []
        try:
            _write(home, files, dest, skip, out)
            return out
        except _ReadFailed as exc:
            log.warning("읽는 중 실패해 묶음에서 뺍니다: %s (%s)", exc.path, exc.reason)
            out.unreadable.append((exc.path, exc.reason))
            skip.add(exc.path)
