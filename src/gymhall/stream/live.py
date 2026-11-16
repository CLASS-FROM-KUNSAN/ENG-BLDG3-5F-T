"""송출 세션 하나: 소리 -> 인코더 -> 서버 -> Tailscale -> 링크 -> 이슈.

순서가 중요합니다.

  1. 인코더와 서버를 먼저 띄우고 min_uptime_sec 동안 실제로 바이트가
     나오는지 봅니다. 장치가 잘못돼 바로 죽는 송출의 링크를 이슈로
     올리면, 받은 사람은 열어 보고 '안 들림' 만 겪습니다.
  2. 그다음 tailscale serve/funnel 을 켜고 링크를 만들어 이슈로 올립니다.
  3. link_check_sec 마다 tailnet 상태를 다시 읽어 링크가 바뀌면 다시 올립니다.
  4. 끝날 때 serve/funnel 을 끄고(teardown) 이슈를 닫습니다.
"""

from __future__ import annotations

import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..logging_setup import get as get_logger
from . import capture, hub, notify, tailnet

log = get_logger("stream.live")


@dataclass
class StreamSettings:
    server: str = "builtin"
    source: str = "system"
    capture: str = "auto"
    device: str = ""
    codec: str = "mp3"
    bitrate_kbps: int = 96
    samplerate: int = 48000
    channels: int = 2
    port: int = 8765
    bind: str = ""
    path: str = "/live"
    player_page: bool = True
    title: str = "실시간 송출"
    max_listeners: int = 5
    client_buffer_kb: int = 512
    require_token: bool = True
    token: str = ""
    token_bytes: int = 16
    max_duration_minutes: float = 0
    idle_stop_minutes: float = 0
    min_uptime_sec: float = 5
    restart_on_failure: int = 2
    # tailscale
    ts_enabled: bool = True
    ts_mode: str = "serve"
    ts_https_port: int = 443
    ts_use_magicdns: bool = True
    ts_allow_funnel: bool = False
    ts_exe: str = ""
    ts_link_check_sec: float = 60
    ts_require_online: bool = True
    ts_override_existing: bool = False
    ts_teardown: bool = True
    # icecast
    ic_host: str = "127.0.0.1"
    ic_port: int = 8000
    ic_mount: str = "/live"
    ic_user: str = "source"
    issue: notify.IssueSettings = field(default_factory=notify.IssueSettings)


class StreamError(Exception):
    pass


def settings_from(cfg) -> StreamSettings:
    s = cfg.section("stream")
    ts = cfg.section("stream.tailscale")
    ic = cfg.section("stream.icecast")
    iss = cfg.section("stream.issue")
    out = StreamSettings()
    for key in ("server", "source", "capture", "device", "codec", "bind", "path", "title", "token"):
        if key in s:
            setattr(out, key, str(s[key]))
    for key in ("bitrate_kbps", "samplerate", "channels", "port", "max_listeners",
                "client_buffer_kb", "token_bytes", "restart_on_failure"):
        if key in s:
            setattr(out, key, int(s[key]))
    for key in ("max_duration_minutes", "idle_stop_minutes", "min_uptime_sec"):
        if key in s:
            setattr(out, key, float(s[key]))
    for key in ("player_page", "require_token"):
        if key in s:
            setattr(out, key, bool(s[key]))
    mapping = {"enabled": "ts_enabled", "mode": "ts_mode", "https_port": "ts_https_port",
               "use_magicdns": "ts_use_magicdns", "allow_funnel": "ts_allow_funnel", "exe": "ts_exe",
               "link_check_sec": "ts_link_check_sec", "require_online": "ts_require_online",
               "override_existing": "ts_override_existing", "teardown": "ts_teardown"}
    for key, attr in mapping.items():
        if key in ts:
            setattr(out, attr, type(getattr(out, attr))(ts[key]))
    for key, attr in {"host": "ic_host", "port": "ic_port", "mount": "ic_mount", "user": "ic_user"}.items():
        if key in ic:
            setattr(out, attr, type(getattr(out, attr))(ic[key]))
    issue = notify.IssueSettings()
    for key in issue.__dataclass_fields__:
        if key in iss:
            value = iss[key]
            setattr(issue, key, list(value) if isinstance(value, list) else value)
    issue.owner = str(issue.owner or cfg.get("github.owner", ""))
    issue.repo = str(issue.repo or cfg.get("github.repo", ""))
    out.issue = issue
    return out


def check(s: StreamSettings) -> None:
    """설정만으로 알 수 있는 거부 사유. 아무것도 띄우기 전에 봅니다."""
    if s.ts_enabled and s.ts_mode == "funnel" and not s.ts_allow_funnel:
        raise StreamError(
            "mode = \"funnel\" 은 인터넷 전체에 공개합니다. 강의 소리를 공개 송출하면\n"
            "  공중송신(저작권 침해)이 될 수 있습니다. 정말 필요하면\n"
            "  [stream.tailscale] allow_funnel = true 를 직접 적으세요."
        )
    if s.server == "icecast" and s.require_token:
        log.warning("Icecast 는 접속 토큰을 지원하지 않습니다. 링크에 토큰 없이 송출합니다 "
                    "(Icecast 쪽 인증을 쓰세요).")


@dataclass
class Session:
    settings: StreamSettings
    label: str = ""
    machine: str = ""
    client: object | None = None     # ReleaseUploader (이슈용). None 이면 이슈 없음
    store: object | None = None
    dry_run: bool = False
    stop_event: threading.Event = field(default_factory=threading.Event)

    # 실행 중 상태
    token: str = ""
    link: str = ""
    started: float = 0.0
    _server: object | None = None
    _hub: hub.Hub | None = None
    _encoder: capture.Encoder | None = None
    _exposed: bool = False
    _ts_exe: str = ""
    _bound: str = ""

    # -- 부품 ---------------------------------------------------------
    def _make_token(self) -> str:
        s = self.settings
        if not s.require_token or s.server == "icecast":
            return ""
        return s.token or pysecrets.token_urlsafe(max(8, s.token_bytes))

    def _local_port(self) -> int:
        return self.settings.ic_port if self.settings.server == "icecast" else self.settings.port

    def _bind_host(self, node: tailnet.Node | None) -> str:
        s = self.settings
        if s.bind:
            return s.bind
        if s.ts_enabled and s.ts_mode == "direct":
            if node is None or not node.ipv4:
                raise StreamError("direct 모드인데 이 기기의 Tailscale IPv4 를 모릅니다.")
            # 0.0.0.0 이 아니라 Tailscale IP 에만 엽니다. 카페 와이파이의
            # 옆 사람이 같은 포트로 들어오지 못하게 합니다.
            return node.ipv4
        return "127.0.0.1"

    def _encoder_cmd(self) -> tuple[list[str], object | None]:
        from ..recorder.ffmpeg_util import find_ffmpeg

        s = self.settings
        spec = capture.EncodeSpec(codec=s.codec, bitrate_kbps=s.bitrate_kbps,
                                  samplerate=s.samplerate, channels=s.channels)
        mode = capture.resolve_capture(s.capture)
        pcm = None
        if mode == "soundcard":
            from ..recorder import devices

            resolver = devices.resolve_system_source if s.source == "system" else devices.resolve_mic_source
            pcm = resolver(s.device)
            inp = None
        else:
            inp = capture.ffmpeg_input_args(source=s.source, device=s.device)
        if s.server == "icecast":
            from .. import secrets

            password = secrets.get("icecast", required=False) or ""
            if not password:
                raise StreamError("Icecast 소스 비밀번호가 없습니다.  python -m gymhall secrets set icecast")
            out = capture.icecast_url(host=s.ic_host, port=s.ic_port, mount=s.ic_mount,
                                      user=s.ic_user, password=password)
            ctype = hub.CONTENT_TYPES[s.codec]
        else:
            out, ctype = "pipe:1", ""
        return capture.build_ffmpeg(spec, exe=find_ffmpeg(), input_args=inp,
                                    output=out, content_type=ctype), pcm

    def _start_encoder(self) -> None:
        s = self.settings
        cmd, pcm = self._encoder_cmd()
        on_chunk = self._hub.publish if self._hub is not None else None
        self._encoder = capture.Encoder(
            cmd, pcm_source=pcm, on_chunk=on_chunk,
            spec=capture.EncodeSpec(codec=s.codec, bitrate_kbps=s.bitrate_kbps,
                                    samplerate=s.samplerate, channels=s.channels))
        self._encoder.start()

    def _node(self) -> tailnet.Node | None:
        if not self.settings.ts_enabled:
            return None
        return tailnet.status(self._ts_exe)

    def _link_for(self, node: tailnet.Node | None) -> str:
        s = self.settings
        path = s.ic_mount if s.server == "icecast" else s.path
        if node is None:
            return f"http://127.0.0.1:{self._local_port()}{'/' + path.lstrip('/')}" + (
                f"?t={self.token}" if self.token else "")
        return tailnet.build_link(node, mode=s.ts_mode, port=self._local_port(),
                                  https_port=s.ts_https_port, path=path,
                                  use_magicdns=s.ts_use_magicdns, token=self.token)

    def _info(self, node: tailnet.Node | None, link: str) -> notify.LinkInfo:
        s = self.settings
        player = ""
        if s.server == "builtin" and s.player_page:
            base = link.split("?", 1)[0]
            root = base[: base.rfind("/")] + "/"
            player = root + (f"?t={self.token}" if self.token else "")
        return notify.LinkInfo(
            link=link, player=player, host=node.dns_name if node else "127.0.0.1",
            tailnet=node.tailnet if node else "", mode=s.ts_mode if node else "local",
            label=self.label, machine=self.machine, source=s.source, codec=s.codec,
            bitrate=s.bitrate_kbps, started=datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M"),
            token=self.token,
        )

    # -- 실행 ---------------------------------------------------------
    def run(self, notifier: notify.IssueNotifier | None = None) -> int:  # noqa: C901
        s = self.settings
        check(s)
        node = None
        if s.ts_enabled:
            self._ts_exe = tailnet.find_exe(s.ts_exe)
            node = self._node()
            if s.ts_require_online and not (node and node.running and node.online):
                raise StreamError(
                    f"Tailscale 이 연결돼 있지 않습니다(상태: {getattr(node, 'backend_state', '?')}). "
                    "tailscale up 으로 로그인하세요."
                )

        self.token = self._make_token()
        self.started = time.time()
        reason = "사용자가 멈춤"

        try:
            if s.server == "builtin":
                self._hub = hub.Hub(max_listeners=s.max_listeners, client_buffer_kb=s.client_buffer_kb)
                self._open_server(self._bind_host(node))

            self._start_encoder()
            if not self._wait_healthy(s.min_uptime_sec):
                reason = "인코더가 시작하자마자 멈춤"
                log.error("송출이 시작되지 않았습니다: %s. 장치 설정을 확인하세요 "
                          "(python -m gymhall devices).", self._encoder.error or "ffmpeg 종료")
                return 1

            if node is not None:
                self._exposed = tailnet.expose(self._ts_exe, s.ts_mode, s.ts_https_port,
                                               self._local_port(),
                                               override_existing=s.ts_override_existing)
            self.link = self._link_for(node)
            print(f"\n듣기: {self.link}")
            info = self._info(node, self.link)
            if info.player:
                print(f"재생 페이지: {info.player}")
            print("멈추려면 Ctrl+C\n")
            log.info("송출 링크: %s", tailnet.redact(self.link))
            if notifier is not None and node is not None:
                notifier.link_created(info, reason="start")

            reason = self._loop(notifier, node)
            return 0
        except KeyboardInterrupt:
            reason = "사용자가 멈춤"
            return 0
        finally:
            self._teardown()
            if notifier is not None:
                notifier.stopped(reason=reason, minutes=(time.time() - self.started) / 60,
                                 listeners=self._hub.total_listeners if self._hub else 0)

    def _open_server(self, host: str) -> None:
        s = self.settings
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._server = hub.make_server(self._hub, host=host, port=s.port, path=s.path,
                                       codec=s.codec, token=self.token,
                                       player_page=s.player_page, title=s.title)
        self._bound = host
        threading.Thread(target=self._server.serve_forever, name="stream-http",
                         daemon=True).start()
        log.info("내장 오디오 서버: http://%s:%d%s", host, s.port, s.path)

    def _wait_healthy(self, seconds: float) -> bool:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if not self._encoder.alive:
                return False
            time.sleep(0.25)
        if not self._encoder.alive:
            return False
        if self._hub is not None and seconds > 0 and self._hub.bytes_in == 0:
            self._encoder.error = "인코더에서 소리가 한 바이트도 나오지 않음"
            return False
        return True

    def _loop(self, notifier, node) -> str:
        s = self.settings
        restarts = 0
        next_check = time.time() + s.ts_link_check_sec
        idle_since = time.time()
        offline_logged = False
        while not self.stop_event.wait(1.0):
            now = time.time()
            if s.max_duration_minutes > 0 and now - self.started >= s.max_duration_minutes * 60:
                return f"최대 길이 {s.max_duration_minutes:g}분 도달"

            if self._hub is not None:
                if self._hub.count > 0:
                    idle_since = now
                elif s.idle_stop_minutes > 0 and now - idle_since >= s.idle_stop_minutes * 60:
                    return f"청취자 없이 {s.idle_stop_minutes:g}분 지남"

            if not self._encoder.alive:
                if restarts >= s.restart_on_failure:
                    return f"인코더가 멈춤({self._encoder.error or 'ffmpeg 종료'}), 재시작 {restarts}회 실패"
                restarts += 1
                log.warning("인코더가 멈췄습니다. 다시 시작합니다 (%d/%d)", restarts, s.restart_on_failure)
                self._encoder.stop()
                self._start_encoder()

            if node is not None and s.ts_link_check_sec > 0 and now >= next_check:
                next_check = now + s.ts_link_check_sec
                try:
                    fresh = self._node()
                except Exception as exc:
                    log.warning("Tailscale 상태를 읽지 못했습니다: %s", exc)
                    continue
                if not (fresh and fresh.running and fresh.online):
                    if not offline_logged:
                        log.warning("Tailscale 연결이 끊겼습니다. 돌아오면 링크를 다시 확인합니다.")
                        offline_logged = True
                    continue
                offline_logged = False
                # direct 모드는 서버가 Tailscale IP 에 묶여 있습니다. IP 가 바뀌면
                # 링크만 고쳐서는 안 열립니다 — 새 IP 로 다시 엽니다.
                if (self._hub is not None and s.ts_mode == "direct" and not s.bind
                        and fresh.ipv4 and fresh.ipv4 != self._bound):
                    log.info("Tailscale IP 가 바뀌어 서버를 %s 로 다시 엽니다.", fresh.ipv4)
                    self._open_server(fresh.ipv4)
                link = self._link_for(fresh)
                if link != self.link:
                    log.info("송출 링크가 바뀌었습니다: %s -> %s",
                             tailnet.redact(self.link), tailnet.redact(link))
                    self.link = link
                    print(f"\n새 링크: {link}")
                    if notifier is not None:
                        notifier.link_created(self._info(fresh, link), reason="change")
        return "중지 요청"

    def _teardown(self) -> None:
        s = self.settings
        if self._encoder is not None:
            self._encoder.stop()
        if self._hub is not None:
            self._hub.close()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        if self._exposed and s.ts_teardown:
            tailnet.unexpose(self._ts_exe, s.ts_mode, s.ts_https_port)
