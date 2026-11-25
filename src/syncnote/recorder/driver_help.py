"""메인보드(베이스보드) 제조사·모델을 읽어 오디오 드라이버 검색을 Chromium 으로 띄웁니다.

기본 녹음 장치가 '스테레오 믹스' 이고 마이크 입력이 없을 때(devices.mix_only) 사람이 직접
드라이버를 설치·업데이트해야 합니다. 강의실 PC 에서 실제로는 Windows 기본 드라이버만 잡혀
'Realtek Audio Console 이 이 기계에서 지원되지 않습니다' 였고, 메인보드 제조사(GIGABYTE
H410M HD3P)의 Realtek 드라이버를 설치해야 했습니다. 그래서 msinfo32 의 '베이스보드 제조업체'·
'베이스보드 제품' 과 같은 값(Win32_BaseBoard)을 읽어 Google 에서 그 보드의 드라이버를 찾아
보여 줍니다. 드라이버를 대신 받거나 실행하지는 않습니다.

브라우저는 사람이 실행한 명령(devices.bat·doctor.bat·setup.bat)에서만 띄웁니다 — 녹화·송출·
작업 스케줄러가 띄운 것에서는 링크만 로그에 남깁니다(수업 중 화면에 창이 뜨지 않게).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

from ..logging_setup import get as get_logger

log = get_logger("recorder.driver_help")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 의미 없는 기본값(제조사가 채우지 않은 보드)
_JUNK = re.compile(r"^(to be filled.*|default string|not applicable|none|n/?a|system product name|"
                   r"base board|baseboard|\s*)$", re.I)


def baseboard() -> tuple[str, str]:
    """(제조사, 모델). Windows 가 아니거나 읽지 못하면 ("", ""). 예외 없음."""
    if sys.platform != "win32":
        return "", ""
    from ..exec_policy import _powershell, ps_env

    cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
           "$b = Get-CimInstance Win32_BaseBoard -ErrorAction Stop | Select-Object -First 1; "
           "'{0}|{1}' -f $b.Manufacturer, $b.Product")
    try:
        cp = subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", cmd],
                            capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                            env=ps_env(), creationflags=_NO_WINDOW)
        line = cp.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
    except Exception as exc:  # noqa: BLE001 — 못 읽으면 일반 검색으로
        log.debug("베이스보드 정보를 읽지 못했습니다: %s", exc)
        return "", ""
    maker, _, product = line.partition("|")
    clean = [("" if _JUNK.match(x.strip()) else x.strip()) for x in (maker, product)]
    return clean[0], clean[1]


def short_maker(maker: str) -> str:
    """'Gigabyte Technology Co., Ltd.' → 'Gigabyte' (검색어가 깔끔하게)."""
    m = re.split(r"\s+(?:technology|computer|electronics|international|corp\.?|co\.?,?|inc\.?|ltd\.?)\b",
                 maker, maxsplit=1, flags=re.I)[0]
    return m.strip(" ,.") or maker.strip()


# 모델 이름에 이미 제조사가 들어 있으면 그것을 믿습니다. 강의실 PC 는 제조사 칸이 'ASUSTeK
# COMPUTER INC.' 인데 모델이 'Gigabyte B360M DS3H' 였습니다 — 둘을 붙이면 엉뚱한 검색이 됩니다.
_BRANDS = re.compile(r"\b(asus|asustek|gigabyte|msi|asrock|biostar|evga|supermicro|intel|lenovo|"
                     r"dell|hp|samsung|lg|acer|colorful|maxsun|jginyue)\b", re.I)


def board_label(maker: str, product: str) -> str:
    if product and _BRANDS.search(product):
        return product
    return " ".join(x for x in (short_maker(maker) if maker else "", product) if x)


def search_url(maker: str, product: str) -> str:
    if product:
        q = f"{board_label(maker, product)} Realtek audio driver"
    else:
        q = "Realtek HD Audio driver 다운로드"
    return "https://www.google.com/search?q=" + quote_plus(q)


def chromium_exe() -> Path | None:
    """playwright install chromium 이 받아 둔 Chromium(가장 새 판). 없으면 None."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom and custom != "0":
        root = Path(custom)
    else:
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ms-playwright"
    found = []
    for d in root.glob("chromium-*"):
        m = re.fullmatch(r"chromium-(\d+)", d.name)
        if not m:
            continue                       # chromium_headless_shell-* 은 창이 없음
        for sub in ("chrome-win64", "chrome-win"):
            exe = d / sub / "chrome.exe"
            if exe.is_file():
                found.append((int(m.group(1)), exe))
                break
    return max(found)[1] if found else None


def open_search(url: str) -> str:
    """Chromium 으로 엽니다(없으면 기본 브라우저). 무엇으로 열었는지, 못 열었으면 ""."""
    exe = chromium_exe()
    if exe is not None:
        try:
            flags = (_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen([str(exe), "--new-window", url], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
            return "Chromium"
        except OSError as exc:
            log.debug("Chromium 을 띄우지 못했습니다: %s", exc)
    # Chromium 이 없으면 Windows 의 기본 브라우저(URL 연결 프로그램)로
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        try:
            startfile(url)
            return "기본 브라우저"
        except OSError as exc:
            log.debug("브라우저를 띄우지 못했습니다: %s", exc)
    return ""
