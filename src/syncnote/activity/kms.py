"""DEK(데이터 암호화 키) 관리 — at-rest 봉인.

저장소는 무작위 32바이트 DEK 로 AES-GCM 암호화하고, 그 DEK 자체는 디스크에
**평문으로 두지 않습니다.** 이번 증분은 Windows DPAPI(CryptProtectData, 현재
사용자+기기 바인딩)로 DEK 를 봉인합니다. 게이트가 이미 TPM·Secure Boot 를 검증한
기기에서만 DEK 가 생성되므로, DPAPI 마스터키도 그 플랫폼 보호 아래 있습니다.

정직하게: TPM 비추출 CNG 키(Microsoft Platform Crypto Provider)로 DEK 를 감싸는
것은 **다음 하드닝**입니다. DEK 를 분리해 둔 덕에 그 업그레이드는 저장소 포맷을
건드리지 않습니다(같은 DEK 를 다른 방식으로 봉인만 바꿈).
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

DEK_BYTES = 32  # AES-256


class KmsError(Exception):
    pass


# -- Windows DPAPI (외부 의존성 없음) -----------------------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi(func, data: bytes) -> bytes:
    if os.name != "nt":
        raise KmsError("DPAPI 는 Windows 전용입니다. 이 기기에서는 활동 저장을 지원하지 않습니다.")
    in_blob, out_blob = _blob(data), _BLOB()
    # CRYPTPROTECT_UI_FORBIDDEN(0x1): UI 없이, 백그라운드에서도 안전하게.
    if not func(ctypes.byref(in_blob), None, None, None, None, 0x1, ctypes.byref(out_blob)):
        raise KmsError(f"DPAPI 호출 실패 (code {ctypes.get_last_error()})")
    try:
        return _blob_bytes(out_blob)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def seal(data: bytes) -> bytes:
    return _dpapi(ctypes.windll.crypt32.CryptProtectData, data)


def unseal(sealed: bytes) -> bytes:
    return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, sealed)


# -- DEK 로드/생성 ------------------------------------------------------------
def load_or_create(dek_path: str | Path) -> bytes:
    """봉인된 DEK 파일을 읽거나, 없으면 새 DEK 를 만들어 봉인 저장합니다.

    호출 전에 게이트를 통과했다고 가정합니다(SealedStore 가 그 순서를 지킵니다).
    """
    path = Path(dek_path)
    if path.exists():
        dek = unseal(path.read_bytes())
        if len(dek) != DEK_BYTES:
            raise KmsError(f"DEK 길이가 이상합니다({len(dek)}바이트). 파일이 손상됐을 수 있습니다.")
        return dek
    dek = os.urandom(DEK_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(seal(dek))      # 평문 DEK 는 디스크에 닿지 않음 — 봉인본만 씀
    tmp.replace(path)
    return dek
