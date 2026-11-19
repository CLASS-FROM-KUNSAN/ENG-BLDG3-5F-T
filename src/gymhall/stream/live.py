"""송출 세션 하나: 소리 -> 인코더 -> 서버 -> Tailscale -> 링크 -> 이슈.

원칙 세 가지.

  1. **시간표가 최우선입니다.** config.toml 의 [[recording.timetable]] 수업
     시간 안이면 세션은 그 수업이 끝나는 시각에 끝나고, 그 전에는 다른
     어떤 조건(청취자 없음, 최대 송출 시간)으로도 멈추지 않습니다. 수업
     밖에서 손으로 켠 송출이 수업 시간에 들어가면 그 수업을 넘겨받습니다.
  2. **멈추지 말고 되돌립니다(롤백).** 인코더가 죽거나, 장치가 사라지거나,
     tailscale serve 설정이 없어지거나, 최대 송출 시간에 닿으면 세션을
     끝내지 않고 '마지막으로 잘 돌던 상태' 로 되돌려 다시 띄웁니다.
     토큰·링크·이슈는 그대로라 청취자는 다시 연결만 하면 됩니다.
     실패하면 간격을 늘려 가며(backoff) 계속 시도합니다. 횟수 제한은
     기본으로 없습니다(restart_on_failure = 0).
  3. **소리가 실제로 나온 뒤에만 링크를 올립니다.** 장치가 틀려 바로 죽는
     송출의 링크가 이슈로 올라가면 받은 사람은 '안 들림' 만 겪습니다.
"""

from __future__ import annotations

import secrets as pysecrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..logging_setup import get as get_logger
from ..recorder import schedule as sched
from . import capture, hub, notify, tailnet

log = get_logger("stream.live")

ON_MAX_DURATION = ("rollback", "stop")


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
    max_listeners: int = 0
    client_buffer_kb: int = 2048
    require_token: bool = True
    token: str = ""
    token_bytes: int = 16
    # 멈춤·복구
    max_duration_minutes: float = 0
    on_max_duration: str = "rollback"
    idle_stop_minutes: float = 0
    min_uptime_sec: float = 5
    restart_on_failure: int = 0          # 0 = 무제한
    restart_backoff_sec: float = 5
    restart_backoff_max_sec: float = 120
    fallback_default_device: bool = True
    # 시간표
    follow_timetable: bool = True
    timetable_slots: list[str] = field(default_factory=list)
    pre_roll_minutes: float = 0
    post_roll_minutes: float = 0
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


_STR = ("server", "source", "capture", "device", "codec", "bind", "path", "title", "token",
        "on_max_duration")
_INT = ("bitrate_kbps", "samplerate", "channels", "port", "max_listeners", "client_buffer_kb",
        "token_bytes", "restart_on_failure")
_FLOAT = ("max_duration_minutes", "idle_stop_minutes", "min_uptime_sec", "restart_backoff_sec",
          "restart_backoff_max_sec", "pre_roll_minutes", "post_roll_minutes")
_BOOL = ("player_page", "require_token", "fallback_default_device", "follow_timetable")


def settings_from(cfg) -> StreamSettings:
    s = cfg.section("stream")
    ts = cfg.section("stream.tailscale")
    ic = cfg.section("stream.icecast")
    iss = cfg.section("stream.issue")
    out = StreamSettings()
    for keys, kind in ((_STR, str), (_INT, int), (_FLOAT, float), (_BOOL, bool)):
        for key in keys:
            if key in s:
                setattr(out, key, kind(s[key]))
    if "timetable_slots" in s:
        out.timetable_slots = [str(x) for x in s["timetable_slots"] or []]
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
    if s.on_max_duration not in ON_MAX_DURATION:
        raise StreamError(f"on_max_duration 은 {ON_MAX_DURATION} 중 하나여야 합니다.")
    if s.server == "icecast" and s.require_token:
        log.warning("Icecast 는 접속 토큰을 지원하지 않습니다. 링크에 토큰 없이 송출합니다 "
                    "(Icecast 쪽 인증을 쓰세요).")


# ---------------------------------------------------------------------------
# 시간표
# ---------------------------------------------------------------------------
def timetable(cfg, s: StreamSettings) -> list[sched.Slot]:
    """송출이 따를 수업들. timetable_slots 가 있으면 그 이름만."""
    slots = sched.parse(cfg.get("recording.timetable", []))
    if s.timetable_slots:
        wanted = {n.strip() for n in s.timetable_slots}
        slots = [x for x in slots if x.name in wanted]
    return slots


def active_slot(slots: list[sched.Slot], s: StreamSettings,
                now: datetime | None = None) -> sched.Occurrence | None:
    """지금 진행 중인 수업(앞뒤 여유 포함). 없으면 None."""
    now = now or datetime.now()
    occ = sched.next_occurrence(slots, now=now, pre_roll_min=s.pre_roll_minutes,
                                post_roll_min=s.post_roll_minutes)
    if occ is not None and occ.start <= now < occ.end:
        return occ
    return None


def backoff(attempt: int, base: float, cap: float) -> float:
    """5, 10, 20, 40 ... cap. attempt 는 0 부터."""
    return min(float(cap), float(base) * (2 ** max(0, attempt)))


@dataclass
class Session:
    settings: StreamSettings
    label: str = ""
    machine: str = ""
    dry_run: bool = False
    # 시간표가 정한 끝 시각. 이 시각 전에는 어떤 조건으로도 멈추지 않습니다.
    until: datetime | None = None
    slots: list[sched.Slot] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    tick: float = 1.0                    # 감시 주기(초). 테스트에서 줄입니다.

    # 실행 중 상태
    token: str = ""
    link: str = ""
    started: float = 0.0
    rollbacks: int = 0
    _server: object | None = None
    _server_thread: threading.Thread | None = None
    _hub: hub.Hub | None = None
    _encoder: capture.Encoder | None = None
    _exposed: bool = False
    _ts_exe: str = ""
    _bound: str = ""
    _good_device: str | None = None     # 마지막으로 소리가 실제로 나온 장치
    _segment_started: float = 0.0
    _notifier: notify.IssueNotifier | None = None

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

    def _encoder_cmd(self, device: str) -> tuple[list[str], object | None]:
        from ..recorder.ffmpeg_util import find_ffmpeg

        s = self.settings
        spec = capture.EncodeSpec(codec=s.codec, bitrate_kbps=s.bitrate_kbps,
                                  samplerate=s.samplerate, channels=s.channels)
        mode = capture.resolve_capture(s.capture)
        pcm = None
        if mode == "soundcard":
            from ..recorder import devices

            resolver = devices.resolve_system_source if s.source == "system" else devices.resolve_mic_source
            pcm = resolver(device)
            inp = None
        else:
            inp = capture.ffmpeg_input_args(source=s.source, device=device)
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

    def _start_encoder(self, device: str) -> None:
        s = self.settings
        cmd, pcm = self._encoder_cmd(device)
        on_chunk = self._hub.publish if self._hub is not None else None
        self._encoder = capture.Encoder(
            cmd, pcm_source=pcm, on_chunk=on_chunk,
            spec=capture.EncodeSpec(codec=s.codec, bitrate_kbps=s.bitrate_kbps,
                                    samplerate=s.samplerate, channels=s.channels))
        self._encoder.start()

    def _open_server(self, host: str) -> None:
        s = self.settings
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server = hub.make_server(self._hub, host=host, port=s.port, path=s.path,
                                       codec=s.codec, token=self.token,
                                       player_page=s.player_page, title=s.title)
        self._bound = host
        self._server_thread = threading.Thread(target=self._server.serve_forever,
                                               name="stream-http", daemon=True)
        self._server_thread.start()
        log.info("내장 오디오 서버: http://%s:%d%s", host, s.port, s.path)

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
            token=self.token, until=self.until.strftime("%H:%M") if self.until else "",
        )

    # -- 시간표 --------------------------------------------------------
    def _past_until(self) -> bool:
        return self.until is not None and datetime.now() >= self.until

    def _adopt_slot(self) -> None:
        """수업 밖에서 켠 송출이 수업 시간에 들어가면 그 수업의 끝을 따릅니다."""
        s = self.settings
        if self.until is not None or not s.follow_timetable or not self.slots:
            return
        occ = active_slot(self.slots, s)
        if occ is not None:
            self.until = occ.end
            log.info("시간표의 '%s' 수업 시간에 들어왔습니다. %s 에 송출을 마칩니다.",
                     occ.slot.name, occ.end.strftime("%H:%M"))
            self._note(f"시간표의 **{occ.slot.name}** 시간에 들어와 {occ.end:%H:%M} 에 마칩니다.")

    # -- 복구 ---------------------------------------------------------
    def _note(self, text: str) -> None:
        if self._notifier is not None:
            self._notifier.note(text)

    def _wait_healthy(self, seconds: float) -> bool:
        """seconds 동안 인코더가 살아 있고 실제로 바이트가 나왔는가."""
        start_bytes = self._hub.bytes_in if self._hub is not None else 0
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if not self._encoder.alive or self.stop_event.is_set():
                return False
            time.sleep(0.25)
        if not self._encoder.alive:
            return False
        if self._hub is not None and seconds > 0 and self._hub.bytes_in == start_bytes:
            self._encoder.error = "인코더에서 소리가 한 바이트도 나오지 않음"
            return False
        if (self.settings.source == "mic" and getattr(self._encoder, "pcm_source", None) is not None
                and getattr(self._encoder, "peak", 1.0) == 0.0 and seconds > 0):
            self._encoder.error = "[mic] 레벨이 정확히 0 입니다 (신호 경로 없음)"
            return False
        return True

    def _find_alternate_mic(self, exclude: list[str]) -> str | None:
        """실패한 마이크를 제외하고 살아있는 대체 마이크를 탐색합니다."""
        from ..recorder import devices

        try:
            chosen, _probes = devices.select_working_mic(exclude=exclude)
            if chosen is not None:
                name = getattr(chosen, "name", "")
                if name:
                    return name
        except Exception as exc:
            log.debug("대체 마이크 탐색 실패: %s", exc)
        return None

    def _try_start(self) -> bool:
        """마지막으로 잘 돌던 장치 -> 설정한 장치 -> 기본 장치 순으로 띄웁니다."""
        s = self.settings
        order: list[str] = []
        for dev in (self._good_device, s.device, "" if s.fallback_default_device else None):
            if dev is not None and dev not in order:
                order.append(dev)

        # 사용자가 특정 장치를 강제 고정한 경우(s.device 명시 + fallback_default_device=False)
        # 에는 대체 장치 자동 전환을 차단합니다.
        can_switch_mic = (s.source == "mic" and (not s.device or s.fallback_default_device))

        failed_devices: list[str] = []
        for dev in order:
            if self._encoder is not None:
                self._encoder.stop()
            try:
                self._start_encoder(dev)
            except (capture.CaptureError, StreamError):
                raise  # 설정 문제: 다시 해도 같습니다
            except Exception as exc:
                log.warning("장치 '%s' 를 열지 못했습니다: %s", dev or "(기본)", exc)
                failed_devices.append(dev)
                continue
            if self._wait_healthy(s.min_uptime_sec):
                if self._good_device is not None and dev != self._good_device:
                    log.warning("장치를 '%s' 로 바꿔 복구했습니다.", dev or "(기본)")
                self._good_device = dev
                self._segment_started = time.time()
                return True
            failed_devices.append(dev)
            log.warning("장치 '%s' 로는 소리가 나오지 않습니다: %s",
                        dev or "(기본)", self._encoder.error or "ffmpeg 종료")

        # 기존 복구 체인에서 실패했고, 마이크 자동 전환이 허용된 경우 대체 마이크 탐색
        if can_switch_mic:
            alt_dev = self._find_alternate_mic(exclude=failed_devices)
            if alt_dev is not None and alt_dev not in failed_devices:
                log.info("대체 마이크 탐색: '%s' 시도", alt_dev)
                if self._encoder is not None:
                    self._encoder.stop()
                try:
                    self._start_encoder(alt_dev)
                    if self._wait_healthy(s.min_uptime_sec):
                        log.warning("대체 마이크 '%s' 로 전환하여 복구했습니다.", alt_dev)
                        self._good_device = alt_dev
                        self._segment_started = time.time()
                        return True
                    failed_devices.append(alt_dev)
                    log.warning("대체 마이크 '%s' 로도 소리가 나오지 않습니다: %s",
                                alt_dev, self._encoder.error or "ffmpeg 종료")
                except Exception as exc:
                    failed_devices.append(alt_dev)
                    log.warning("대체 마이크 '%s' 열기 실패: %s", alt_dev, exc)

        return False

    def _recover(self, why: str) -> bool:
        """롤백: 인코더를 마지막으로 잘 돌던 상태로 되돌려 다시 띄웁니다.

        성공할 때까지 간격을 늘려 가며 시도합니다. 멈추는 것은 사용자가
        멈췄을 때, 시간표가 끝났을 때, restart_on_failure(0 = 무제한)를
        넘었을 때뿐입니다.
        """
        s = self.settings
        attempt = 0
        log.warning("롤백: %s", why)
        while not self.stop_event.is_set() and not self._past_until():
            if s.restart_on_failure > 0 and attempt >= s.restart_on_failure:
                return False
            if self._try_start():
                self.rollbacks += 1
                log.info("롤백 완료 (%d번째): %s", self.rollbacks, why)
                if self.settings.issue.comment_on_rollback:
                    self._note(f"롤백으로 복구했습니다 — {why}. 같은 링크로 다시 연결하세요.")
                return True
            wait = backoff(attempt, s.restart_backoff_sec, s.restart_backoff_max_sec)
            attempt += 1
            log.warning("복구 실패 (%d번째). %.0f초 뒤 다시 시도합니다.", attempt, wait)
            self.stop_event.wait(wait)
        return False

    def _wait_online(self) -> tailnet.Node | None:
        """Tailscale 이 붙을 때까지 기다립니다(require_online). 그 사이 송출은 이미 돕니다."""
        logged = False
        while not self.stop_event.is_set() and not self._past_until():
            try:
                node = self._node()
            except tailnet.TailscaleError as exc:
                node = None
                if not logged:
                    log.warning("Tailscale 상태를 읽지 못했습니다: %s", exc)
            if node is not None and node.running and node.online:
                return node
            if not logged:
                log.warning("Tailscale 이 연결되지 않았습니다(%s). 연결되면 링크를 올립니다.",
                            getattr(node, "backend_state", "?"))
                logged = True
            self.stop_event.wait(10)
        return None

    # -- 실행 ---------------------------------------------------------
    def run(self, notifier: notify.IssueNotifier | None = None) -> int:
        s = self.settings
        check(s)
        self._notifier = notifier
        if s.ts_enabled:
            self._ts_exe = tailnet.find_exe(s.ts_exe)
        if s.follow_timetable and self.until is None:
            occ = active_slot(self.slots, s)
            if occ is not None:
                self.until = occ.end
                self.label = self.label or occ.slot.name
                log.info("시간표의 '%s' 수업 중입니다. %s 에 마칩니다.", occ.slot.name,
                         occ.end.strftime("%H:%M"))
        self.label = self.label or f"송출_{datetime.now():%H%M}"

        self.token = self._make_token()
        self.started = time.time()
        reason = "사용자가 멈춤"
        try:
            node = None
            if s.ts_enabled:
                node = self._node()
            if s.server == "builtin":
                self._hub = hub.Hub(max_listeners=s.max_listeners, client_buffer_kb=s.client_buffer_kb)
                try:
                    host = self._bind_host(node)
                except StreamError:
                    # direct 인데 아직 Tailscale IP 가 없음. 붙으면 그 IP 로 다시 엽니다.
                    host = "127.0.0.1"
                self._open_server(host)

            if not self._try_start() and not self._recover("시작 직후 소리가 나오지 않음"):
                reason = "시작하지 못함"
                log.error("송출을 시작하지 못했습니다. 장치 설정을 확인하세요 (python -m gymhall devices).")
                return 1

            if s.ts_enabled and s.ts_require_online and not (node and node.running and node.online):
                node = self._wait_online()
                if node is None:
                    reason = "Tailscale 에 연결되지 못함"
                    return 1
                if self._hub is not None and s.ts_mode == "direct" and not s.bind:
                    self._open_server(self._bind_host(node))

            if node is not None:
                self._exposed = tailnet.expose(self._ts_exe, s.ts_mode, s.ts_https_port,
                                               self._local_port(),
                                               override_existing=s.ts_override_existing)
            self.link = self._link_for(node)
            info = self._info(node, self.link)
            print(f"\n듣기: {self.link}")
            if info.player:
                print(f"재생 페이지: {info.player}")
            if self.until:
                print(f"시간표에 따라 {self.until:%H:%M} 에 마칩니다.")
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
                                 listeners=self._hub.total_listeners if self._hub else 0,
                                 rollbacks=self.rollbacks)

    def _loop(self, notifier, node) -> str:  # noqa: C901 - 감시 항목이 많습니다
        s = self.settings
        next_check = time.time() + s.ts_link_check_sec
        idle_since = time.time()
        offline_logged = False
        while not self.stop_event.wait(self.tick):
            now = time.time()

            # 1) 시간표가 최우선: 수업이 끝나면 끝, 끝나기 전에는 아래 어떤 조건도 멈추지 않음
            if self._past_until():
                return f"시간표 종료 ({self.until:%H:%M})"
            self._adopt_slot()

            # 2) 최대 송출 시간: 기본은 멈추지 않고 롤백(새 인코더로 순환)
            if s.max_duration_minutes > 0 and now - self._segment_started >= s.max_duration_minutes * 60:
                if s.on_max_duration == "stop" and self.until is None:
                    return f"최대 송출 시간 {s.max_duration_minutes:g}분 도달"
                if not self._recover(f"최대 송출 시간 {s.max_duration_minutes:g}분 순환"):
                    return "롤백 실패"

            # 3) 청취자 없음: 시간표 밖에서만, 설정했을 때만
            if self._hub is not None:
                if self._hub.count > 0:
                    idle_since = now
                elif (self.until is None and s.idle_stop_minutes > 0
                      and now - idle_since >= s.idle_stop_minutes * 60):
                    return f"청취자 없이 {s.idle_stop_minutes:g}분 지남"

            # 4) 인코더·서버 상태
            if (not self._encoder.alive
                    and not self._recover(f"인코더가 멈춤 ({self._encoder.error or 'ffmpeg 종료'})")):
                return "롤백 실패" if not self._past_until() else f"시간표 종료 ({self.until:%H:%M})"
            if self._server_thread is not None and not self._server_thread.is_alive():
                log.warning("오디오 서버가 멈췄습니다. 다시 엽니다.")
                self._open_server(self._bound)

            # 5) Tailscale: 링크 변화, serve 설정 사라짐
            if node is None or s.ts_link_check_sec <= 0 or now < next_check:
                continue
            next_check = now + s.ts_link_check_sec
            try:
                fresh = self._node()
            except Exception as exc:
                log.warning("Tailscale 상태를 읽지 못했습니다: %s", exc)
                continue
            if not (fresh and fresh.running and fresh.online):
                if not offline_logged:
                    log.warning("Tailscale 연결이 끊겼습니다. 송출은 계속하고, 돌아오면 링크를 다시 확인합니다.")
                    offline_logged = True
                continue
            offline_logged = False
            if self._exposed and not tailnet.port_in_use(tailnet.serve_status(self._ts_exe),
                                                         s.ts_https_port):
                log.warning("tailscale %s 설정이 사라져 다시 켭니다(롤백).", s.ts_mode)
                try:
                    tailnet.expose(self._ts_exe, s.ts_mode, s.ts_https_port, self._local_port(),
                                   override_existing=True)
                    self.rollbacks += 1
                except tailnet.TailscaleError as exc:
                    log.error("tailscale %s 를 다시 켜지 못했습니다: %s", s.ts_mode, exc)
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
