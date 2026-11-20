"""설치·점검이 실패했을 때 '왜' 를 출력에서 짐작하는 표.

종류(kind)에 따라 설치·점검 창이 다르게 대응합니다.
  transient  잠깐 기다렸다가 같은 동작을 다시(지수 백오프)
  auto       정해 둔 해결책을 하나 적용한 뒤 다시
  user       사용자가 해야 함 — 이유와 방법을 보여 주고 멈춤
  fatal      다시 해도 안 됨 — 이유와 수동 방법을 보여 주고 멈춤

표는 위에서부터 맞춰 보고 처음 맞는 것을 씁니다(더 구체적인 것을 위에).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Cause:
    id: str
    kind: str          # transient | auto | user | fatal
    title: str         # 원인 추정(짧게)
    hint: str          # 사람이 할 일 / 편집기가 할 일
    pattern: re.Pattern


def _p(text: str) -> re.Pattern:
    return re.compile(text, re.I | re.M)


CAUSES: tuple[Cause, ...] = (
    Cause("python_missing", "auto", "파이썬을 찾지 못함(PATH 에 없음 또는 Microsoft Store 가짜 실행 파일)",
          "편집기의 파이썬으로 가상환경을 먼저 만든 뒤 setup.bat 을 다시 돌립니다.",
          _p(r"Python 이 설치돼 있지 않습니다|WindowsApps\\python|was not found; run without arguments "
             r"to install from the Microsoft Store")),
    Cause("disk_full", "user", "디스크 공간 부족",
          "디스크를 비운 뒤 '파일 → 설치 · 점검 다시 실행' 하세요(라이브러리·브라우저에 수 GB).",
          _p(r"No space left on device|WinError 112|디스크 공간이 부족")),
    Cause("long_path", "user", "경로가 너무 김(260자 제한)",
          "Windows '긴 경로 사용'을 켜거나 레포를 짧은 경로(예: C:\\sync)로 옮기세요.",
          _p(r"WinError 206|filename or extension is too long|파일 이름이나 확장명이 너무 깁니다")),
    Cause("ssl", "user", "인증서 검사 실패(학교·회사망이 HTTPS 를 중간에서 검사)",
          "그 망의 루트 인증서를 설치하거나(`pip install pip-system-certs`) 다른 망에서 설치하세요. "
          "검증을 끄는 방법은 쓰지 않습니다.",
          _p(r"CERTIFICATE_VERIFY_FAILED|SSLError|certificate verify failed")),
    Cause("no_wheel", "fatal", "이 파이썬 버전용 패키지가 없음",
          "파이썬 3.11 또는 3.12 를 설치해 그 버전으로 다시 설치하세요(너무 새 버전은 휠이 늦게 나옴).",
          _p(r"No matching distribution found|Could not find a version that satisfies|"
             r"Failed building wheel|error: Microsoft Visual C\+\+ \d+\.\d+ or greater is required")),
    Cause("file_lock", "transient", "파일이 잠김(백신 검사·편집기·다른 프로그램이 쓰는 중)",
          "잠시 뒤 다시 시도합니다. 계속되면 백신 실시간 검사를 잠깐 끄거나 편집기를 닫고 설치하세요.",
          _p(r"WinError (5|32)\b|Access is denied|액세스가 거부|being used by another process|"
             r"다른 프로세스가 파일을 사용")),
    Cause("pip_busy", "transient", "다른 설치(pip)가 이미 돌고 있음",
          "그 설치가 끝나기를 기다렸다가 다시 시도합니다.",
          _p(r"another instance of pip|lock.*pip|pip.*lock")),
    Cause("proxy", "transient", "프록시가 막음",
          "잠시 뒤 다시 시도합니다. 계속되면 HTTPS_PROXY 환경변수(학교망 프록시 주소)를 확인하세요.",
          _p(r"ProxyError|407 Proxy Authentication|Cannot connect to proxy")),
    Cause("network", "transient", "네트워크가 불안정하거나 끊김",
          "잠시 뒤 다시 시도합니다. 계속되면 인터넷 연결·학교망 방화벽을 확인하세요.",
          _p(r"Read timed out|ConnectTimeoutError|Connection (?:aborted|reset|refused|timed out)|"
             r"Temporary failure in name resolution|getaddrinfo failed|NewConnectionError|"
             r"HTTPSConnectionPool|Max retries exceeded|IncompleteRead|ECONNRESET|ETIMEDOUT")),
    Cause("stall", "transient", "출력 없이 오래 멈춤",
          "프로세스를 끊고 다시 시도합니다(다운로드가 멈췄거나 입력을 기다리는 중이었을 수 있음).",
          _p(r"(?!)")),   # 절대 안 맞음 — 출력 문구가 아니라 멈춤 감지로만 씀
)
BY_ID = {c.id: c for c in CAUSES}
UNKNOWN = Cause("unknown", "fatal", "원인을 알 수 없음", "출력 보기에서 첫 오류를 확인하세요.",
                _p(r"(?!)"))      # 절대 안 맞음(빈 출력에도)


def classify(output: str, rc: int = 1) -> Cause | None:
    """출력에서 원인 추정. 성공(rc 0)이고 맞는 것도 없으면 None."""
    for c in CAUSES:
        if c.pattern.search(output or ""):
            return c
    return None if rc == 0 else UNKNOWN


def backoff(attempt: int, base: float = 2.0, cap: float = 30.0) -> float:
    """attempt(1부터) 번째 다시 시도 전에 기다릴 초: 2, 4, 8 … (상한 cap)."""
    return min(cap, base * (2 ** max(0, attempt - 1)))
