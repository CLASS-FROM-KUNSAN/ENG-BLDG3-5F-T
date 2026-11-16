"""실시간 송출: Tailscale 링크, 내장 서버, 인코더 명령, 이슈 알림.

Tailscale·ffmpeg·GitHub 없이 돕니다. 실제 tailnet 송출은 실기에서 봅니다.
"""

from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

import pytest

from gymhall import cli
from gymhall import config as cfgmod
from gymhall.stream import capture, hub, live, notify, tailnet

STATUS = {
    "BackendState": "Running",
    "Self": {
        "HostName": "BOOK",
        "DNSName": "book.tail1234.ts.net.",
        "TailscaleIPs": ["100.88.96.81", "fd7a:115c:a1e0::1"],
        "Online": True,
    },
    "CurrentTailnet": {"Name": "me@example.com"},
}


def node(**kw) -> tailnet.Node:
    base = tailnet.parse_status(STATUS)
    return tailnet.Node(**{**base.__dict__, **kw})


# ---------------------------------------------------------------------------
# tailnet
# ---------------------------------------------------------------------------
def test_parse_status_strips_trailing_dot_and_splits_ips():
    n = tailnet.parse_status(STATUS)
    assert (n.dns_name, n.ipv4, n.ipv6, n.tailnet) == (
        "book.tail1234.ts.net", "100.88.96.81", "fd7a:115c:a1e0::1", "me@example.com")
    assert n.running and n.online


def test_parse_status_of_a_logged_out_node():
    n = tailnet.parse_status({"BackendState": "NeedsLogin"})
    assert not n.running and n.dns_name == "" and n.ipv4 == ""


def test_links_per_mode():
    n = node()
    assert tailnet.build_link(n, mode="serve", port=8765) == "https://book.tail1234.ts.net/live"
    assert tailnet.build_link(n, mode="serve", port=8765, https_port=10000, path="radio") == (
        "https://book.tail1234.ts.net:10000/radio")
    assert tailnet.build_link(n, mode="direct", port=8765, token="abc") == (
        "http://book.tail1234.ts.net:8765/live?t=abc")
    assert tailnet.build_link(n, mode="direct", port=8765, use_magicdns=False) == (
        "http://100.88.96.81:8765/live")


def test_serve_needs_magicdns():
    with pytest.raises(tailnet.TailscaleError, match="MagicDNS"):
        tailnet.build_link(node(dns_name=""), mode="serve", port=1)


def test_token_is_url_encoded_and_redacted():
    link = tailnet.build_link(node(), mode="serve", port=1, token="a+b/c")
    assert link.endswith("?t=a%2Bb%2Fc")
    assert tailnet.redact(link) == "https://book.tail1234.ts.net/live?t=***"


def test_serve_commands_and_busy_port():
    assert tailnet.serve_command("ts", "serve", 443, 8765) == [
        "ts", "serve", "--bg", "--https=443", "http://127.0.0.1:8765"]
    assert tailnet.serve_off_command("ts", "funnel", 8443) == ["ts", "funnel", "--https=8443", "off"]
    # 실기 PC 처럼 443 에 이미 funnel 이 걸려 있으면 덮어쓰지 않아야 합니다.
    busy = {"TCP": {"443": {"HTTPS": True}}}
    assert tailnet.port_in_use(busy, 443) and not tailnet.port_in_use(busy, 10000)
    assert not tailnet.port_in_use({}, 443)


def test_expose_refuses_a_port_someone_else_uses(monkeypatch):
    monkeypatch.setattr(tailnet, "serve_status", lambda exe: {"TCP": {"443": {}}})
    monkeypatch.setattr(tailnet, "_run", lambda *a, **k: pytest.fail("덮어쓰면 안 됩니다"))
    with pytest.raises(tailnet.TailscaleError, match="이미"):
        tailnet.expose("ts", "serve", 443, 8765)
    assert tailnet.expose("ts", "direct", 443, 8765) is False


# ---------------------------------------------------------------------------
# hub
# ---------------------------------------------------------------------------
def test_token_check():
    assert hub.check_token("", "anything")
    assert hub.check_token("s3cret", "s3cret")
    assert not hub.check_token("s3cret", "") and not hub.check_token("s3cret", "nope")


def test_slow_listener_is_dropped_without_blocking_others():
    h = hub.Hub(max_listeners=0, client_buffer_kb=16)  # 16KB = 4칸
    slow, fast = h.join("slow"), h.join("fast")
    for _ in range(10):
        h.publish(b"x" * hub.CHUNK)
        while not fast.q.empty():
            fast.q.get_nowait()
    assert slow.dropped and not fast.dropped


def test_max_listeners_and_close():
    h = hub.Hub(max_listeners=1)
    first = h.join("a")
    assert first is not None and h.join("b") is None
    h.close()
    assert first.q.get_nowait() is None and h.join("c") is None


def _serve(h, token="tok"):
    server = hub.make_server(h, host="127.0.0.1", port=0, path="/live", codec="mp3", token=token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_http_server_streams_and_guards():
    h = hub.Hub()
    server, base = _serve(h)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/live", timeout=3)
        assert exc.value.code == 403

        def feed():
            for _ in range(50):
                if h.count:
                    break
                time.sleep(0.05)
            h.publish(b"ID3-frame")
            h.close()

        threading.Thread(target=feed, daemon=True).start()
        with urllib.request.urlopen(f"{base}/live?t=tok", timeout=5) as resp:
            assert resp.headers["Content-Type"] == "audio/mpeg"
            assert resp.read() == b"ID3-frame"

        page = urllib.request.urlopen(f"{base}/?t=tok", timeout=3).read().decode()
        assert 'src="/live?t=tok"' in page
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------
def test_encoder_reads_pcm_from_stdin_for_soundcard():
    cmd = capture.build_ffmpeg(capture.EncodeSpec(), exe="ff", input_args=None)
    i = cmd.index("-i")
    assert cmd[i - 6:i + 2] == ["-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]
    assert cmd.index("-fflags") < i < cmd.index("-flush_packets")
    assert cmd[-1] == "pipe:1" and "libmp3lame" in cmd


def test_encoder_aac_is_adts_so_late_listeners_can_join():
    cmd = capture.build_ffmpeg(capture.EncodeSpec(codec="aac", bitrate_kbps=64), exe="ff",
                               input_args=["-f", "pulse", "-i", "default"])
    assert cmd[cmd.index("-f", cmd.index("-c:a")) + 1] == "adts" and "64k" in cmd


def test_icecast_output_quotes_the_password():
    url = capture.icecast_url(host="h", port=8000, mount="live", user="source", password="p@ss:w/d")
    assert url == "icecast://source:p%40ss%3Aw%2Fd@h:8000/live"
    cmd = capture.build_ffmpeg(capture.EncodeSpec(), exe="ff", input_args=None, output=url,
                               content_type="audio/mpeg")
    assert cmd[-3:] == ["-content_type", "audio/mpeg", url]


def test_windows_system_sound_via_ffmpeg_is_refused():
    with pytest.raises(capture.CaptureError, match="record start"):
        capture.ffmpeg_input_args(source="system", device="", plat="win32")


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------
class FakeGitHub:
    owner, repo = "o", "r"

    def __init__(self, private=True):
        self.private = private
        self.calls: list[tuple[str, str, dict | None]] = []
        self.issues: dict[int, str] = {}

    def _request(self, method, url, **kw):
        body = kw.get("json")
        self.calls.append((method, url.split("/repos/o/r")[-1], body))
        if method == "GET" and url.endswith("/repos/o/r"):
            return {"private": self.private, "full_name": "o/r", "has_issues": True}
        if method == "GET" and "/labels/" in url:
            return {"name": "x"}
        if method == "POST" and url.endswith("/issues"):
            n = len(self.issues) + 1
            self.issues[n] = "open"
            return {"number": n, "html_url": f"https://x/{n}"}
        if method == "PATCH":
            self.issues[int(url.rsplit("/", 1)[1])] = body["state"]
            return {}
        if method == "GET" and "/issues/" in url:
            return {"state": self.issues.get(int(url.rsplit("/", 1)[1]), "closed")}
        return {}

    def kinds(self):
        return [(m, p.split("?")[0]) for m, p, _ in self.calls if m != "GET"]


class MemStore:
    def __init__(self):
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def save(self):
        pass


def info(link="https://h/live?t=tok", **kw):
    return notify.LinkInfo(link=link, token="tok", label="자료구조", **kw)


def test_render_fills_known_keeps_unknown_and_hides_token():
    s = notify.IssueSettings(include_token=False)
    out = notify.render("{label} {link} {oops}", info(), s, "처음")
    assert out == "자료구조 https://h/live?t=*** {oops}"
    body = notify.render(notify.DEFAULT_BODY, info(), s, "처음")
    assert "tok" not in body.replace("token", "") and "따로" in body


def test_first_link_opens_an_issue_with_labels_and_mentions():
    gh = FakeGitHub()
    s = notify.IssueSettings(mentions=["@me"], assignees=["me"])
    n = notify.IssueNotifier(s, client=gh, store=MemStore())
    assert n.link_created(info(), reason="start") == 1
    _, _, payload = next(c for c in gh.calls if c[0] == "POST" and c[1] == "/issues")
    assert payload["labels"] == ["live-stream"] and payload["assignees"] == ["me"]
    assert payload["body"].startswith("@me") and "https://h/live?t=tok" in payload["body"]


def test_public_repo_is_refused_by_default():
    gh = FakeGitHub(private=False)
    n = notify.IssueNotifier(notify.IssueSettings(), client=gh)
    assert n.link_created(info(), reason="start") is None
    assert gh.kinds() == []


def test_change_new_opens_and_closes_previous():
    gh = FakeGitHub()
    n = notify.IssueNotifier(notify.IssueSettings(), client=gh, store=MemStore())
    n.link_created(info("https://a/live"), reason="start")
    assert n.link_created(info("https://b/live"), reason="change") == 2
    assert gh.issues == {1: "closed", 2: "open"}


def test_change_comment_keeps_one_issue():
    gh = FakeGitHub()
    n = notify.IssueNotifier(notify.IssueSettings(on_change="comment"), client=gh, store=MemStore())
    n.link_created(info("https://a/live"), reason="start")
    assert n.link_created(info("https://b/live"), reason="change") == 1
    assert ("POST", "/issues/1/comments") in gh.kinds() and len(gh.issues) == 1


def test_change_none_touches_nothing():
    gh = FakeGitHub()
    n = notify.IssueNotifier(notify.IssueSettings(on_change="none"), client=gh)
    n.link_created(info("https://a/live"), reason="start")
    before = list(gh.kinds())
    n.link_created(info("https://b/live"), reason="change")
    assert gh.kinds() == before


def test_dedupe_reuses_an_open_issue_for_the_same_link():
    gh, store = FakeGitHub(), MemStore()
    notify.IssueNotifier(notify.IssueSettings(), client=gh, store=store).link_created(
        info(), reason="start")
    again = notify.IssueNotifier(notify.IssueSettings(), client=gh, store=store)
    assert again.link_created(info(), reason="start") == 1 and len(gh.issues) == 1


def test_stop_comments_and_closes():
    gh = FakeGitHub()
    n = notify.IssueNotifier(notify.IssueSettings(), client=gh, store=MemStore())
    n.link_created(info(), reason="start")
    n.stopped(reason="최대 길이", minutes=50, listeners=3)
    assert ("POST", "/issues/1/comments") in gh.kinds() and gh.issues[1] == "closed"


def test_github_failure_never_stops_the_stream():
    class Broken(FakeGitHub):
        def _request(self, *a, **k):
            raise RuntimeError("boom")

    n = notify.IssueNotifier(notify.IssueSettings(require_private_repo=False), client=Broken())
    assert n.link_created(info(), reason="start") is None


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _cfg(override: dict) -> cfgmod.Config:
    return cfgmod.Config(data=cfgmod._deep_merge(cfgmod.DEFAULTS, override))


def test_defaults_are_valid_and_map_into_settings():
    cfg = _cfg({"github": {"owner": "o", "repo": "r"}})
    s = live.settings_from(cfg)
    assert (s.ts_mode, s.port, s.codec, s.issue.repo, s.issue.labels) == (
        "serve", 8765, "mp3", "r", ["live-stream"])


@pytest.mark.parametrize("override,needle", [
    ({"tailscale": {"mode": "funnel"}}, "allow_funnel"),
    ({"tailscale": {"mode": "funnel", "allow_funnel": True, "https_port": 9000}}, "8443"),
    ({"path": "live"}, "'/'"),
    ({"path": "/status"}, "/status"),
    ({"port": 70000}, "65535"),
    ({"token": "short"}, "12자"),
    ({"issue": {"on_change": "spam"}}, "on_change"),
    ({"issue": {"label_color": "red"}}, "label_color"),
])
def test_bad_stream_settings_are_rejected(override, needle):
    with pytest.raises(cfgmod.ConfigError, match=needle):
        cfgmod.validate(_cfg({"stream": override}))


def test_funnel_without_permission_is_refused_at_runtime_too():
    s = live.StreamSettings(ts_mode="funnel")
    with pytest.raises(live.StreamError, match="allow_funnel"):
        live.check(s)


def test_cli_flags_override_config():
    cfg = _cfg({})
    args = cli.build_parser().parse_args(
        ["stream", "start", "--mode", "direct", "--port", "9000", "--codec", "aac",
         "-m", "50", "--no-issue"])
    s = cli._stream_settings(cfg, args)
    assert (s.ts_mode, s.port, s.codec, s.max_duration_minutes, s.issue.enabled) == (
        "direct", 9000, "aac", 50.0, False)


# ---------------------------------------------------------------------------
# 시간표 최우선 + 롤백
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta  # noqa: E402

from gymhall.recorder import schedule as sched  # noqa: E402


class FakeEncoder:
    """살아 있다가 kill() 하면 죽는 인코더. 시작될 때마다 기록합니다."""

    started: list[str] = []

    def __init__(self, device, healthy=True):
        self.device, self.healthy, self.error = device, healthy, None
        self._alive = True
        FakeEncoder.started.append(device)

    @property
    def alive(self):
        return self._alive

    def kill(self):
        self._alive = False

    def stop(self):
        self._alive = False


def fake_session(monkeypatch, *, bad_devices=(), **kw) -> live.Session:
    FakeEncoder.started = []
    s = live.StreamSettings(ts_enabled=False, server="icecast", min_uptime_sec=0,
                            restart_backoff_sec=0.01, restart_backoff_max_sec=0.02,
                            **kw.pop("settings", {}))
    sess = live.Session(settings=s, tick=0.01, **kw)

    def start(self, device):
        if device in bad_devices:
            raise OSError(f"장치 없음: {device}")
        self._encoder = FakeEncoder(device)

    monkeypatch.setattr(live.Session, "_start_encoder", start)
    monkeypatch.setattr(live.Session, "_wait_healthy", lambda self, sec: self._encoder.alive)
    return sess


def test_backoff_doubles_up_to_the_cap():
    assert [live.backoff(i, 5, 60) for i in range(6)] == [5, 10, 20, 40, 60, 60]


def test_timetable_filter_and_active_slot():
    cfg = cfgmod.Config(data=cfgmod._deep_merge(cfgmod.DEFAULTS, {"recording": {"timetable": [
        {"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:50"},
        {"name": "영어", "day": "mon", "start": "11:00", "end": "12:00"},
    ]}}))
    s = live.StreamSettings(timetable_slots=["자료구조"], pre_roll_minutes=5)
    slots = live.timetable(cfg, s)
    assert [x.name for x in slots] == ["자료구조"]
    monday = datetime(2026, 11, 16)  # 월요일
    assert live.active_slot(slots, s, monday.replace(hour=8, minute=56)).slot.name == "자료구조"
    assert live.active_slot(slots, s, monday.replace(hour=8, minute=50)) is None
    assert live.active_slot(slots, s, monday.replace(hour=11, minute=30)) is None


def test_recover_rolls_back_to_the_last_good_device_then_default(monkeypatch):
    sess = fake_session(monkeypatch, bad_devices=("USB 마이크",),
                        settings={"device": "USB 마이크"})
    sess._good_device = "USB 마이크"
    assert sess._recover("장치가 사라짐")
    # 마지막 장치(=설정 장치)가 없으니 기본 장치로 내려가 복구합니다.
    assert sess._encoder.device == "" and sess._good_device == "" and sess.rollbacks == 1


def test_recover_is_unlimited_by_default(monkeypatch):
    sess = fake_session(monkeypatch)
    fails = {"n": 0}

    def flaky(self, device):
        fails["n"] += 1
        if fails["n"] < 6:
            raise OSError("아직 안 됨")
        self._encoder = FakeEncoder(device)

    monkeypatch.setattr(live.Session, "_start_encoder", flaky)
    assert sess._recover("테스트") and fails["n"] >= 6


def test_recover_respects_an_explicit_limit(monkeypatch):
    sess = fake_session(monkeypatch, bad_devices=("",), settings={"restart_on_failure": 3})
    assert not sess._recover("테스트")


def test_config_errors_are_not_retried_forever(monkeypatch):
    sess = fake_session(monkeypatch)

    def broken(self, device):
        raise capture.CaptureError("Windows 에서 루프백 불가")

    monkeypatch.setattr(live.Session, "_start_encoder", broken)
    with pytest.raises(capture.CaptureError):
        sess._recover("테스트")


def test_dead_encoder_is_rolled_back_not_stopped(monkeypatch):
    sess = fake_session(monkeypatch, until=datetime.now() + timedelta(seconds=0.4))
    assert sess._try_start()
    first = sess._encoder
    threading.Timer(0.05, first.kill).start()
    reason = sess._loop(None, None)
    assert reason.startswith("시간표 종료")
    assert sess.rollbacks >= 1 and sess._encoder is not first


def test_max_duration_rolls_over_instead_of_stopping(monkeypatch):
    sess = fake_session(monkeypatch, until=datetime.now() + timedelta(seconds=0.5),
                        settings={"max_duration_minutes": 0.1 / 60})
    assert sess._try_start()
    assert sess._loop(None, None).startswith("시간표 종료")
    assert sess.rollbacks >= 2


def test_timetable_beats_stop_conditions(monkeypatch):
    # 'stop' 으로 둬도, 청취자가 없어도, 시간표 수업 중에는 끝 시각까지 갑니다.
    sess = fake_session(monkeypatch, until=datetime.now() + timedelta(seconds=0.4),
                        settings={"max_duration_minutes": 0.05 / 60, "on_max_duration": "stop",
                                  "idle_stop_minutes": 0.01 / 60})
    sess._hub = hub.Hub()
    assert sess._try_start()
    started = time.time()
    assert sess._loop(None, None).startswith("시간표 종료")
    assert time.time() - started >= 0.3


def test_stop_conditions_still_work_outside_the_timetable(monkeypatch):
    sess = fake_session(monkeypatch, settings={"max_duration_minutes": 0.05 / 60,
                                               "on_max_duration": "stop"})
    assert sess._try_start()
    assert sess._loop(None, None).startswith("최대 송출 시간")


def test_manual_stream_adopts_a_class_that_starts(monkeypatch):
    now = datetime.now()
    slot = sched.Slot(name="운영체제", day=list(sched.DAY_INDEX)[now.weekday()],
                      start=f"{now:%H:%M}", end=f"{(now + timedelta(minutes=2)):%H:%M}")
    sess = fake_session(monkeypatch, slots=[slot])
    sess._adopt_slot()
    assert sess.until is not None and sess.until > now


def test_cli_minutes_means_stop_but_config_means_rollback():
    cfg = _cfg({"stream": {"max_duration_minutes": 60}})
    plain = cli._stream_settings(cfg, cli.build_parser().parse_args(["stream", "start"]))
    assert plain.on_max_duration == "rollback"
    flagged = cli._stream_settings(cfg, cli.build_parser().parse_args(["stream", "start", "-m", "5"]))
    assert flagged.on_max_duration == "stop" and flagged.max_duration_minutes == 5


def test_defaults_put_no_limits_on_the_stream():
    s = live.settings_from(_cfg({}))
    assert (s.max_duration_minutes, s.idle_stop_minutes, s.restart_on_failure,
            s.max_listeners, s.on_max_duration, s.follow_timetable) == (0, 0, 0, 0, "rollback", True)


def test_unknown_timetable_slot_name_is_rejected():
    with pytest.raises(cfgmod.ConfigError, match="timetable_slots"):
        cfgmod.validate(_cfg({"stream": {"timetable_slots": ["없는과목"]}}))


def test_stop_comment_reports_rollbacks():
    gh = FakeGitHub()
    n = notify.IssueNotifier(notify.IssueSettings(), client=gh, store=MemStore())
    n.link_created(info(), reason="start")
    n.stopped(reason="시간표 종료", minutes=100, listeners=2, rollbacks=3)
    comment = next(b["body"] for m, p, b in gh.calls if p == "/issues/1/comments")
    assert "롤백 복구 3회" in comment
