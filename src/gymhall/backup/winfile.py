"""정리(prune)가 파일을 지우기 직전에 쓰는 Windows 전용 도구.

1. open_for_delete: 마지막 확인(해시)과 지우기를 **같은 핸들 하나로** 합니다.

   크기·수정 시각을 다시 보는 것만으로는 '확인 직후, 지우기 직전' 에 저장된
   내용을 못 막습니다(재검토 D3). 처음에는 쓰기만 막고 경로로 다시 읽고
   경로로 지웠는데, 그러면 다른 프로그램이 그 사이 파일을 **이름을 바꿔
   치우고** 같은 이름으로 새 내용을 쓸 수 있었습니다(Vim/Emacs/Word 의
   '이름 바꾸고 새로 쓰기' 저장). 경로로 지우면 그 새 파일이 지워졌습니다
   (4차 검토 재현).

   그래서 읽기+삭제 권한으로 열고 **읽기만 공유**합니다. 공유 검사는
   양방향이라
     - 누가 이미 쓰기/삭제(이름 바꾸기 포함)로 열고 있으면 우리가 못 엶 ->
       지우지 않음(다음 정리에서 다시),
     - 우리가 쥐고 있는 동안에는 아무도 쓰거나 이름을 바꾸거나 지우지 못함.
   해시는 이 핸들로 읽고, 지우기는 이 핸들에 '닫을 때 삭제' 를 표시합니다
   (SetFileInformationByHandle). 경로를 다시 쓰지 않으므로 확인한 그
   파일만 지워집니다.

2. extra_streams: NTFS 대체 데이터 스트림. 백업은 본문만 올리는데, 파일을
   지우면 스트림도 같이 사라집니다. 다운로드 출처 표시(Zone.Identifier)
   말고 다른 스트림이 있으면 지우지 않습니다.

Windows 가 아니면 open_for_delete 는 None 을 내놓고 extra_streams 는 [] 입니다.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 지워도 되는 스트림. 이름은 대소문자를 가리지 않습니다.
_HARMLESS_STREAMS = {"::$data", ":zone.identifier:$data"}
_CHUNK = 8 * 1024 * 1024

if IS_WINDOWS:  # pragma: no cover - Windows 에서만
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x1
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _FILE_DISPOSITION_INFO = 4
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _ERROR_HANDLE_EOF = 38

    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
    _k32.SetFileInformationByHandle.restype = wintypes.BOOL

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

    def _last_error(path: Path) -> OSError:
        err = ctypes.get_last_error()
        return OSError(None, ctypes.FormatError(err).strip(), str(path), err)


def _long(path: Path) -> str:
    """260자를 넘는 경로도 열리게 \\\\?\\ 를 붙입니다.

    resolve() 하지 않습니다 — 링크를 따라가 버리면 FILE_FLAG_OPEN_REPARSE_POINT
    가 무의미해집니다. 호출자가 이미 확인한 실제 경로를 넘깁니다.
    """
    s = str(Path(path).absolute())
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


class DeleteHandle:
    """open_for_delete 가 내놓는 것. 이 핸들로 읽고, 이 핸들로 지웁니다."""

    def __init__(self, path: Path, handle: int, fd: int) -> None:
        self.path = Path(path)
        self._handle = handle
        self.fd = fd
        self.marked = False

    def size(self) -> int:
        return os.fstat(self.fd).st_size

    def sha256(self) -> str:
        os.lseek(self.fd, 0, os.SEEK_SET)
        h = hashlib.sha256()
        while True:
            block = os.read(self.fd, _CHUNK)
            if not block:
                return h.hexdigest()
            h.update(block)

    def mark_delete(self) -> None:
        """핸들을 닫을 때 지워지게 합니다. 실패하면 OSError(읽기 전용 속성 등)."""
        flag = ctypes.c_ubyte(1)  # pragma: no cover - Windows 에서만
        if not _k32.SetFileInformationByHandle(self._handle, _FILE_DISPOSITION_INFO,
                                               ctypes.byref(flag), 1):
            raise _last_error(self.path)
        self.marked = True


@contextlib.contextmanager
def open_for_delete(path: Path) -> Iterator[DeleteHandle | None]:
    """아무도 쓰거나 이름을 바꾸거나 지우지 못하게 쥡니다. 못 쥐면 OSError.

    블록을 나올 때 핸들을 닫고, mark_delete 했으면 그때 지워집니다.
    """
    if not IS_WINDOWS:
        yield None
        return
    handle = _k32.CreateFileW(  # pragma: no cover - Windows 에서만
        _long(path), _GENERIC_READ | _DELETE, _FILE_SHARE_READ, None, _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_SEQUENTIAL_SCAN, None)
    if handle in (None, _INVALID_HANDLE):  # pragma: no cover
        raise _last_error(path)
    try:  # pragma: no cover
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:  # pragma: no cover
        _k32.CloseHandle(handle)
        raise
    try:  # pragma: no cover
        yield DeleteHandle(path, handle, fd)
    finally:  # pragma: no cover
        os.close(fd)                     # 핸들도 같이 닫힙니다


def extra_streams(path: Path) -> list[str]:
    """본문과 Zone.Identifier 말고 다른 NTFS 스트림 이름들. 못 읽으면 OSError."""
    if not IS_WINDOWS:
        return []
    data = _StreamData()  # pragma: no cover - Windows 에서만
    handle = _k32.FindFirstStreamW(_long(path), 0, ctypes.byref(data), 0)
    if handle in (None, _INVALID_HANDLE):  # pragma: no cover
        if ctypes.get_last_error() == _ERROR_HANDLE_EOF:   # 스트림이 하나도 없음
            return []
        raise _last_error(path)
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
