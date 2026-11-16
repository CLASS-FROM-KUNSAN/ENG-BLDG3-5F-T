"""내장 오디오 서버: 인코딩된 바이트를 여러 청취자에게 나눠 보냅니다.

Icecast 같은 별도 서버 없이 표준 라이브러리(http.server)만으로 돕니다.
mp3 / aac(ADTS) 는 프레임마다 동기 신호가 있어서, 청취자가 중간에
들어와도 다음 프레임부터 바로 재생됩니다. 그래서 헤더를 따로 들고
있을 필요가 없습니다(ogg/opus 는 그게 안 돼서 고르지 않았습니다).

느린 청취자 하나가 전체를 막으면 안 됩니다. 청취자마다 큐를 따로
두고, 큐가 가득 차면(=client_buffer_kb 만큼 밀리면) 그 청취자만
끊습니다. 송출은 계속됩니다.

  GET /live?t=<토큰>     오디오 스트림
  GET /?t=<토큰>         간단한 재생 페이지 (player_page = true 일 때)
  GET /status?t=<토큰>   JSON 상태
"""

from __future__ import annotations

import hmac
import html
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from ..logging_setup import get as get_logger

log = get_logger("stream.hub")

CONTENT_TYPES = {"mp3": "audio/mpeg", "aac": "audio/aac"}
CHUNK = 4096


@dataclass
class _Listener:
    addr: str
    q: queue.Queue
    since: float = field(default_factory=time.time)
    dropped: bool = False


class Hub:
    """인코더 출력 -> 청취자 큐들."""

    def __init__(self, *, max_listeners: int = 5, client_buffer_kb: int = 512) -> None:
        self.max_listeners = int(max_listeners)
        # 큐 한 칸 = CHUNK 바이트. 최소 4칸은 둡니다.
        self.slots = max(4, int(client_buffer_kb) * 1024 // CHUNK)
        self._lock = threading.Lock()
        self._listeners: list[_Listener] = []
        self.bytes_in = 0
        self.closed = False
        self.total_listeners = 0

    # -- 청취자 --------------------------------------------------------
    def join(self, addr: str) -> _Listener | None:
        with self._lock:
            if self.closed:
                return None
            if self.max_listeners > 0 and len(self._listeners) >= self.max_listeners:
                return None
            lst = _Listener(addr=addr, q=queue.Queue(maxsize=self.slots))
            self._listeners.append(lst)
            self.total_listeners += 1
        log.info("청취자 접속: %s (현재 %d명)", addr, self.count)
        return lst

    def leave(self, lst: _Listener) -> None:
        with self._lock:
            if lst in self._listeners:
                self._listeners.remove(lst)
        log.info("청취자 나감: %s (현재 %d명)%s", lst.addr, self.count,
                 " — 너무 느려서 끊었습니다" if lst.dropped else "")

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._listeners)

    # -- 인코더 쪽 -----------------------------------------------------
    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.bytes_in += len(chunk)
        with self._lock:
            listeners = list(self._listeners)
        for lst in listeners:
            if lst.dropped:
                continue
            try:
                lst.q.put_nowait(chunk)
            except queue.Full:
                lst.dropped = True
                try:
                    lst.q.put_nowait(None)  # 깨워서 끝내게
                except queue.Full:
                    pass

    def close(self) -> None:
        with self._lock:
            self.closed = True
            listeners = list(self._listeners)
        for lst in listeners:
            try:
                lst.q.put_nowait(None)
            except queue.Full:
                lst.dropped = True


def check_token(expected: str, given: str) -> bool:
    if not expected:
        return True
    return hmac.compare_digest(expected.encode(), (given or "").encode())


def player_html(title: str, stream_path: str, token: str) -> str:
    src = stream_path + (f"?t={html.escape(token, quote=True)}" if token else "")
    t = html.escape(title)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}}
audio{{width:100%}}</style></head>
<body><h1>{t}</h1>
<audio controls autoplay preload="none" src="{src}"></audio>
<p>소리가 안 나면 재생 버튼을 누르세요. 끊기면 새로고침하세요.</p>
</body></html>"""


def make_server(hub: Hub, *, host: str, port: int, path: str, codec: str, token: str,
                player_page: bool = True, title: str = "실시간 송출") -> ThreadingHTTPServer:
    stream_path = "/" + path.lstrip("/")
    ctype = CONTENT_TYPES[codec]

    class Handler(BaseHTTPRequestHandler):
        server_version = "gymhall-stream"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A003 - http.server 의 이름
            log.debug("%s %s", self.address_string(), fmt % args)

        def _deny(self, code: int, msg: str) -> None:
            body = msg.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server 의 이름
            url = urlsplit(self.path)
            given = (parse_qs(url.query).get("t") or [""])[0]
            if not check_token(token, given):
                self._deny(403, "토큰이 맞지 않습니다.")
                return
            if url.path == stream_path:
                self._stream()
            elif url.path == "/status":
                body = json.dumps({"listeners": hub.count, "bytes": hub.bytes_in,
                                   "codec": codec}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/" and player_page:
                body = player_html(title, stream_path, given).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._deny(404, "없는 경로입니다.")

        def _stream(self) -> None:
            # X-Forwarded-For: tailscale serve 를 거치면 접속자가 127.0.0.1 로 보입니다.
            who = self.headers.get("X-Forwarded-For") or self.client_address[0]
            who = f"{who} {self.headers.get('Tailscale-User-Login', '')}".strip()
            lst = hub.join(who)
            if lst is None:
                self._deny(503, "청취자 수가 가득 찼거나 송출이 끝났습니다.")
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = lst.q.get()
                    if chunk is None:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                hub.leave(lst)

    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server
