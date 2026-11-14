"""홈 디렉터리에서 올릴 파일 찾기.

무엇을 빼는가:
  - 홈 바로 아래의 AppData 와 '.' 으로 시작하는 디렉터리(설정·캐시 덩어리).
    더 깊은 곳의 .git, node_modules, .venv 는 **넣습니다**(사용자 결정).
  - 리parse 포인트(정션/심볼릭 링크)는 어느 깊이에서든 따라가지 않습니다.
    홈에는 'Application Data', '시작 메뉴' 같은 호환용 정션이 있어서,
    따라가면 AppData 를 다른 이름으로 다시 훑거나 무한히 돕니다.
  - 클라우드 자리표시자(OneDrive '온라인 전용'). 읽는 순간 내려받기가
    시작됩니다 — 백업이 수 GB 를 내려받게 만들면 안 됩니다.
  - 민감한 이름(레지스트리 하이브, 키, 비밀번호 DB, .env, config.toml 등).

속성 검사는 stat 결과만 받는 작은 함수로 나눠 두었습니다. 리눅스에는
st_file_attributes 가 없으므로(0 으로 봅니다) 테스트가 가짜 속성을
넣어 확인합니다.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import DEFAULTS
from ..logging_setup import get as get_logger

log = get_logger("backup")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_MASK = (FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN
               | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)

# 기본 민감 이름 목록은 설정 기본값 한 곳에만 둡니다(사본이 갈라지면 한쪽만 고쳐집니다).
DEFAULT_EXCLUDE_NAMES: list[str] = list(DEFAULTS["backup"]["home"]["exclude_names"])

# 홈 바로 아래에서만 통째로 빼는 디렉터리(대소문자 무시).
TOP_LEVEL_SKIP = {"appdata"}


@dataclass
class ScanResult:
    files: list[tuple[Path, int, int]] = field(default_factory=list)  # (경로, 크기, mtime_ns)
    excluded: list[tuple[Path, str]] = field(default_factory=list)
    unreadable: list[tuple[Path, str]] = field(default_factory=list)


def file_attributes(st: Any) -> int:
    """Windows 파일 속성. 다른 OS 의 stat 에는 없으므로 0."""
    try:
        return int(getattr(st, "st_file_attributes", 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_reparse_point(st: Any) -> bool:
    return bool(file_attributes(st) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_cloud_placeholder(st: Any) -> bool:
    return bool(file_attributes(st) & _CLOUD_MASK)


def is_sensitive(name: str, patterns: Iterable[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, str(p).lower()) for p in patterns if str(p))


def _utf8_ok(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _under(path: Path, roots: list[Path]) -> bool:
    for r in roots:
        try:
            path.relative_to(r)
            return True
        except ValueError:
            continue
    return False


Report = Callable[[Path, str], None]


def _ignore(_path: Path, _reason: str) -> None:
    return None


def iter_home(home: Path, *, exclude_names: Iterable[str] | None = None,
              skip_dirs: Iterable[Path] = (), on_excluded: Report | None = None,
              on_unreadable: Report | None = None) -> Iterator[tuple[Path, int, int]]:
    """home 아래 올릴 후보 파일을 하나씩 내놓습니다(경로, 크기, mtime_ns).

    66만 개를 리스트로 모으지 않습니다. 한 번에 들고 있는 것은 디렉터리
    하나의 항목과 아직 안 들어간 디렉터리 스택뿐입니다.

    순서는 결정적입니다: 디렉터리마다 이름순으로 그 안의 파일을 먼저,
    그다음 하위 디렉터리를 이름순으로(깊이 우선).

    skip_dirs 는 통째로 뺄 디렉터리(예: gymhall 데이터 폴더). 절대 경로로 비교합니다.
    """
    home = Path(home)
    patterns = list(DEFAULT_EXCLUDE_NAMES if exclude_names is None else exclude_names)
    skips = [Path(os.path.abspath(p)) for p in skip_dirs]  # noqa: PTH100 - resolve 는 정션을 따라갑니다
    excluded = on_excluded or _ignore
    unreadable = on_unreadable or _ignore

    stack: list[tuple[Path, int]] = [(home, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            unreadable(folder, f"디렉터리를 열 수 없음: {exc.__class__.__name__}")
            continue
        subdirs: list[Path] = []
        for entry in entries:
            path = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
                link = entry.is_symlink()
            except OSError as exc:
                unreadable(path, f"stat 실패: {exc.__class__.__name__}")
                continue
            if link or is_reparse_point(st):
                excluded(path, "리parse 포인트(정션/링크)")
                continue
            name = entry.name
            # NTFS 는 짝 없는 서로게이트가 든 이름을 허용합니다. UTF-8 로 못 쓰면
            # SQLite 조회·zip 항목 이름에서 예외가 나 매일 실행이 통째로 죽습니다.
            if not _utf8_ok(name):
                unreadable(path, "UTF-8 로 나타낼 수 없는 이름(짝 없는 서로게이트)")
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as exc:
                unreadable(path, f"종류 확인 실패: {exc.__class__.__name__}")
                continue
            if is_dir:
                if depth == 0 and (name.lower() in TOP_LEVEL_SKIP or name.startswith(".")):
                    excluded(path, "최상위 제외 디렉터리")
                    continue
                if is_sensitive(name, patterns):
                    excluded(path, "민감한 이름")
                    continue
                if skips and _under(path, skips):
                    excluded(path, "gymhall 데이터 폴더")
                    continue
                subdirs.append(path)
                continue
            if not is_file:
                continue  # 장치 파일 등
            if is_sensitive(name, patterns):
                excluded(path, "민감한 이름")
                continue
            if is_cloud_placeholder(st):
                excluded(path, "클라우드 자리표시자(내려받기 방지)")
                continue
            yield path, int(st.st_size), int(st.st_mtime_ns)
        # 스택이라 거꾸로 넣어야 이름순으로 꺼납니다.
        stack.extend((d, depth + 1) for d in reversed(subdirs))


def scan_home(home: Path, *, exclude_names: Iterable[str] | None = None,
              skip_dirs: Iterable[Path] = ()) -> ScanResult:
    """iter_home 을 리스트로 모은 것(작은 홈·테스트용). 상대 경로순으로 정렬합니다."""
    home = Path(home)
    out = ScanResult()
    out.files = list(iter_home(
        home, exclude_names=exclude_names, skip_dirs=skip_dirs,
        on_excluded=lambda p, r: out.excluded.append((p, r)),
        on_unreadable=lambda p, r: out.unreadable.append((p, r)),
    ))
    out.files.sort(key=lambda t: t[0].relative_to(home).as_posix())
    return out
