"""Tailscale: 이 기기의 tailnet 주소를 읽고, 송출 링크를 만듭니다.

세 가지 노출 방식이 있습니다(stream.tailscale.mode).

  direct : 오디오 서버를 이 기기의 Tailscale IP 에 직접 엽니다.
           http://<기기>.<tailnet>.ts.net:8765/live
           설치·설정이 가장 적지만 평문 HTTP 이고, Windows 방화벽이
           들어오는 연결을 막을 수 있습니다.
  serve  : 서버는 127.0.0.1 에만 열고 `tailscale serve` 가 tailnet 안에
           HTTPS 로 내보냅니다.  https://<기기>.<tailnet>.ts.net/live   [권장]
           인증서를 Tailscale 이 붙여 주고, 같은 tailnet 사람만 닿습니다.
  funnel : `tailscale funnel` 로 **인터넷 전체**에 공개합니다.
           강의 소리는 교수와 학교의 저작물입니다. 공개 송출은 사적
           이용이 아니라 공중송신이 됩니다. allow_funnel = true 로
           명시하지 않으면 거부합니다.

링크는 '기기 DNS 이름 또는 IP + 포트 + 경로 + (토큰)' 입니다. 기기 이름을
바꾸거나, tailnet 을 옮기거나, IP 가 바뀌면 링크가 바뀝니다. live.py 가
주기적으로 다시 읽어 바뀌면 새 링크로 이슈를 올립니다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from ..logging_setup import get as get_logger

log = get_logger("stream.tailnet")

MODES = ("direct", "serve", "funnel")

_WINDOWS_DEFAULT = Path(r"C:\Program Files\Tailscale\tailscale.exe")
_MAC_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")


class TailscaleError(Exception):
    """사용자가 고칠 수 있는 Tailscale 문제."""


@dataclass(frozen=True)
class Node:
    """이 기기의 tailnet 상의 모습."""

    backend_state: str = ""
    online: bool = False
    dns_name: str = ""      # book-xxx.tail1234.ts.net (끝의 점 제거)
    host_name: str = ""
    ipv4: str = ""
    ipv6: str = ""
    tailnet: str = ""

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"


def find_exe(configured: str = "") -> str:
    for candidate in (configured, os.environ.get("TAILSCALE"), "tailscale"):
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    for p in (_WINDOWS_DEFAULT, _MAC_APP):
        if p.exists():
            return str(p)
    raise TailscaleError(
        "tailscale 명령을 찾지 못했습니다. https://tailscale.com/download 에서 설치하거나\n"
        "  [stream.tailscale] exe 에 전체 경로를 적으세요."
    )


def _run(exe: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
    cmd = [exe, *args]
    log.debug("실행: %s", subprocess.list2cmdline(cmd))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=flags)


def parse_status(data: dict) -> Node:
    """`tailscale status --json` 의 결과에서 필요한 것만 뽑습니다(순수 함수)."""
    self_ = data.get("Self") or {}
    ips = [str(ip) for ip in (self_.get("TailscaleIPs") or data.get("TailscaleIPs") or [])]
    return Node(
        backend_state=str(data.get("BackendState", "")),
        online=bool(self_.get("Online", False)),
        dns_name=str(self_.get("DNSName", "")).rstrip("."),
        host_name=str(self_.get("HostName", "")),
        ipv4=next((ip for ip in ips if "." in ip), ""),
        ipv6=next((ip for ip in ips if ":" in ip), ""),
        tailnet=str((data.get("CurrentTailnet") or {}).get("Name", "")),
    )


def status(exe: str) -> Node:
    proc = _run(exe, "status", "--json")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise TailscaleError(f"tailscale status 를 읽지 못했습니다: {proc.stderr.strip()[:200]}") from exc
    return parse_status(data)


# ---------------------------------------------------------------------------
# 링크
# ---------------------------------------------------------------------------
def public_host(node: Node, *, use_magicdns: bool = True) -> str:
    if use_magicdns and node.dns_name:
        return node.dns_name
    if node.ipv4:
        return node.ipv4
    raise TailscaleError("이 기기의 Tailscale 주소를 알 수 없습니다(tailscale up 을 했나요?).")


def build_link(node: Node, *, mode: str, port: int, https_port: int = 443, path: str = "/live",
               use_magicdns: bool = True, token: str = "") -> str:
    """청취자가 열 주소. 순수 함수라 테스트에서 그대로 확인합니다."""
    if mode not in MODES:
        raise TailscaleError(f"알 수 없는 mode: {mode!r} (가능: {', '.join(MODES)})")
    path = "/" + path.lstrip("/")
    query = f"?{urlencode({'t': token})}" if token else ""
    if mode == "direct":
        return f"http://{public_host(node, use_magicdns=use_magicdns)}:{port}{path}{query}"
    if not node.dns_name:
        raise TailscaleError(
            "serve/funnel 은 MagicDNS 이름이 필요합니다. 관리 콘솔에서 MagicDNS 와 HTTPS 인증서를 켜세요."
        )
    suffix = "" if https_port == 443 else f":{https_port}"
    return f"https://{node.dns_name}{suffix}{path}{query}"


def redact(link: str) -> str:
    """로그에 남길 때 토큰을 가립니다."""
    if "?t=" not in link:
        return link
    return link.split("?t=", 1)[0] + "?t=***"


# ---------------------------------------------------------------------------
# serve / funnel
# ---------------------------------------------------------------------------
def serve_command(exe: str, mode: str, https_port: int, local_port: int) -> list[str]:
    """`tailscale serve|funnel --bg --https=443 http://127.0.0.1:8765`."""
    return [exe, mode, "--bg", f"--https={https_port}", f"http://127.0.0.1:{local_port}"]


def serve_off_command(exe: str, mode: str, https_port: int) -> list[str]:
    return [exe, mode, f"--https={https_port}", "off"]


def port_in_use(serve_status: dict, https_port: int) -> bool:
    """`tailscale serve status --json` 에 이 HTTPS 포트가 이미 걸려 있나."""
    tcp = (serve_status or {}).get("TCP") or {}
    return str(https_port) in {str(k) for k in tcp}


def serve_status(exe: str) -> dict:
    proc = _run(exe, "serve", "status", "--json")
    try:
        return json.loads(proc.stdout or "{}") or {}
    except json.JSONDecodeError:
        return {}


def expose(exe: str, mode: str, https_port: int, local_port: int, *,
           override_existing: bool = False) -> bool:
    """serve/funnel 을 켭니다. 켰으면 True(나중에 끌 책임이 있음)."""
    if mode == "direct":
        return False
    if port_in_use(serve_status(exe), https_port) and not override_existing:
        raise TailscaleError(
            f"tailscale serve 의 HTTPS {https_port} 포트를 이미 다른 것이 쓰고 있습니다.\n"
            f"  덮어쓰려면 [stream.tailscale] override_existing = true, 아니면 https_port 를 바꾸세요.\n"
            f"  현재 상태: tailscale serve status"
        )
    proc = _run(exe, *serve_command(exe, mode, https_port, local_port)[1:])
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[:400]
        raise TailscaleError(f"tailscale {mode} 실패 (code {proc.returncode}):\n  {msg}")
    log.info("tailscale %s 켬: https %d -> 127.0.0.1:%d", mode, https_port, local_port)
    return True


def unexpose(exe: str, mode: str, https_port: int) -> None:
    if mode == "direct":
        return
    try:
        proc = _run(exe, *serve_off_command(exe, mode, https_port)[1:])
        if proc.returncode != 0:
            log.warning("tailscale %s 끄기 실패: %s", mode, (proc.stderr or "").strip()[:200])
        else:
            log.info("tailscale %s 끔 (https %d)", mode, https_port)
    except Exception as exc:  # 정리 단계라 삼킵니다
        log.warning("tailscale %s 끄기 중 오류: %s", mode, exc)
