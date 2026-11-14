"""정리(prune)가 파일을 지우기 직전에 쓰는 Windows 전용 도구.

1. write_guard: 지우기 전 마지막 확인(해시)부터 지울 때까지 **다른 프로그램이
   쓰지 못하게** 파일을 쥐고 있습니다.

   크기·수정 시각을 다시 보는 것만으로는 '확인 직후, 지우기 직전' 에 저장된
   내용을 못 막습니다(재검토 D3). 그래서 공유 모드에 쓰기를 빼고 엽니다.
   공유 검사는 양방향이라
     - 누가 이미 쓰기로 열고 있으면 우리가 열 때 공유 위반 -> 지우지 않음,
     - 우리가 쥐고 있는 동안 누가 쓰기로 열려고 하면 그쪽이 공유 위반
   이 됩니다. 그 프로그램의 저장은 '사용 중' 오류로 실패하고 내용은 그
   프로그램 안에 남습니다 — 조용히 사라지는 것보다 낫습니다.

   우리 핸들은 읽기 권한만 가지고 읽기·삭제를 공유합니다. 그래서 같은 경로를
   평범하게 읽어 해시하고(sha256_file), 경로로 지울(unlink) 수 있습니다.

2. extra_streams: NTFS 대체 데이터 스트림. 백업은 본문만 올리는데, 파일을
   지우면 스트림도 같이 사라집니다. 다운로드 출처 표시(Zone.Identifier)
   말고 다른 스트림이 있으면 지우지 않습니다.

Windows 가 아니면 둘 다 아무 일도 하지 않습니다.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 지워도 되는 스트림. 이름은 대소문자를 가리지 않습니다.
_HARMLESS_STREAMS = {"::$data", ":zone.identifier:$data"}

if IS_WINDOWS:  # pragma: no cover - Windows 에서만
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_DELETE = 0x4
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _ERROR_HANDLE_EOF = 38

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    class _StreamData(ctypes.Structure):
        _fields_ = [("StreamSize", ctypes.c_longlong),
                    ("cStreamName", ctypes.c_wchar * (260 + 36))]

    _k32.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p,
                                      wintypes.DWORD]
    _k32.FindFirstStreamW.restype = wintypes.HANDLE
    _k32.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.FindNextStreamW.restype = wintypes.BOOL
    _k32.FindClose.argtypes = [wintypes.HANDLE]
    _k32.FindClose.restype = wintypes.BOOL


def _long(path: Path) -> str:
    """260자를 넘는 경로도 열리게 \\\\?\\ 를 붙입니다."""
    s = str(Path(path).resolve())
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


@contextlib.contextmanager
def write_guard(path: Path) -> Iterator[None]:
    """쥐고 있는 동안 아무도 path 에 쓰지 못합니다. 못 쥐면 OSError."""
    if not IS_WINDOWS:
        yield
        return
    handle = _k32.CreateFileW(  # pragma: no cover - Windows 에서만
        _long(path), _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_DELETE, None,
        _OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle in (None, _INVALID_HANDLE):  # pragma: no cover
        err = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(err).strip(), str(path), err)
    try:  # pragma: no cover
        yield
    finally:  # pragma: no cover
        _k32.CloseHandle(handle)


def extra_streams(path: Path) -> list[str]:
    """본문과 Zone.Identifier 말고 다른 NTFS 스트림 이름들. 못 읽으면 OSError."""
    if not IS_WINDOWS:
        return []
    data = _StreamData()  # pragma: no cover - Windows 에서만
    handle = _k32.FindFirstStreamW(_long(path), 0, ctypes.byref(data), 0)
    if handle in (None, _INVALID_HANDLE):  # pragma: no cover
        err = ctypes.get_last_error()
        if err == _ERROR_HANDLE_EOF:      # 스트림이 하나도 없음(드묾)
            return []
        raise OSError(None, ctypes.FormatError(err).strip(), str(path), err)
    out: list[str] = []
    try:  # pragma: no cover
        while True:
            name = data.cStreamName
            if name.lower() not in _HARMLESS_STREAMS:
                out.append(name)
            if not _k32.FindNextStreamW(handle, ctypes.byref(data)):
                break
    finally:  # pragma: no cover
        _k32.FindClose(handle)
    return out
