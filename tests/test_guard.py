"""녹화 지킴이: 녹화 중이 아닐 때 모든 녹화를 '확인됨' 으로 모으고, 틀어지면 되돌려 다시."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from syncnote import config as C
from syncnote.paths import Layout
from syncnote.recorder import guard as G
from syncnote.recorder import outbox

STEM = "20261123_090000_part001"


class World:
    """가짜 바깥세상: 검사·업로드·릴리스를 장난감으로."""

    def __init__(self, root: Path):
        self.root = root
        self.t = 1_000_000.0
        self.busy = None
        self.bad: set[str] = set()          # 검사에 걸리는 파일 이름
        self.rebuild_ok = True
        self.records: dict[str, dict] = {}
        self.remote_assets: dict[str, int] = {}
        self.remote_unknown = False
        self.upload_error: Exception | None = None
        self.calls: list[str] = []

    def hooks(self) -> G.Hooks:
        def verify(p):
            self.calls.append(f"verify {p.name}")
            return "끝부분을 풀 수 없습니다" if p.name in self.bad else None

        def rebuild(p, sound_only=True):
            self.calls.append(f"rebuild {p.name} sound_only={sound_only}")
            if not self.rebuild_ok:
                raise RuntimeError("재료 없음")
            p.write_bytes(p.read_bytes() + b"fixed")
            self.bad.discard(p.name)
            return "다시 담기"

        def upload(p):
            self.calls.append(f"upload {p.name}")
            if self.upload_error:
                raise self.upload_error
            d = f"sha-{p.name}-{p.stat().st_size}"
            self.records[d] = {"tag": "recordings-2026-11-23", "asset": f"PC_{p.name}",
                               "size": p.stat().st_size}
            self.remote_assets[f"PC_{p.name}"] = p.stat().st_size
            return self.records[d]

        def remote(p, rec):
            self.calls.append(f"remote {p.name}")
            if self.remote_unknown:
                return None
            return self.remote_assets.get(rec["asset"]) == p.stat().st_size

        return G.Hooks(
            busy=lambda: self.busy, recover=lambda: self.calls.append("recover"),
            verify=verify, rebuild=rebuild,
            digest=lambda p: f"sha-{p.name}-{p.stat().st_size}",
            record=lambda d: self.records.get(d), upload=upload, remote=remote,
            forget=lambda d: self.records.pop(d, None))

    def guard(self, **kw) -> G.Guard:
        return G.Guard(self.root, G.Ledger(self.root.parent / "ledger.json"), self.hooks(),
                       now=lambda: self.t, **kw)


@pytest.fixture
def w(tmp_path):
    root = tmp_path / "recordings"
    f = root / "2026-11-23" / "자료구조" / f"{STEM}.mkv"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"mkv-data")
    return World(root)


def _f(w) -> Path:
    return w.root / "2026-11-23" / "자료구조" / f"{STEM}.mkv"


def _e(g) -> G.Entry:
    return next(iter(g.ledger.entries.values()))


def test_happy_path_reaches_confirmed_in_one_check(w):
    g = w.guard()
    s = g.tick()
    assert _e(g).state == G.CONFIRMED and s.counts[G.CONFIRMED] == 1
    assert w.calls[0] == "recover" and f"upload {STEM}.mkv" in w.calls
    w.calls.clear()
    g.tick()
    assert not any(c.startswith(("upload", "verify", "remote")) for c in w.calls)   # 다시 안 함


def test_busy_does_nothing(w):
    w.busy = "녹화 중"
    s = w.guard().tick()
    assert s.skipped == "녹화 중" and w.calls == []


def test_broken_file_is_rebuilt_then_uploaded(w):
    w.bad.add(f"{STEM}.mkv")
    g = w.guard()
    g.tick()
    e = _e(g)
    assert e.state == G.CONFIRMED and f"rebuild {STEM}.mkv sound_only=True" in w.calls
    assert any("다시 담기" in h for h in e.history)


def test_unfixable_file_backs_off_then_quarantines_and_retries_daily(w):
    w.bad.add(f"{STEM}.mkv")
    w.rebuild_ok = False
    g = w.guard(max_attempts=3)
    g.tick()
    e = _e(g)
    assert e.state == G.FILE and e.attempts == 1 and e.next_try == w.t + 5 * 60
    g.tick()
    assert e.attempts == 1                                   # 기다리는 중
    for minutes in (5, 15):
        w.t += minutes * 60
        g.tick()
    assert e.state == G.QUARANTINE and e.attempts == 3 and e.next_try == w.t + 24 * 3600
    w.rebuild_ok = True
    w.t += 24 * 3600
    g.tick()
    assert e.state == G.CONFIRMED and e.attempts == 0       # 격리가 풀리면 머물던 단계부터


def test_missing_on_release_rolls_back_and_reuploads(w):
    g = w.guard()
    g.tick()
    w.remote_assets.clear()                                  # 누가 릴리스에서 지움
    e = _e(g)
    e.state = G.UPLOADED                                     # 다음 확인에서 원격을 다시 봄
    g.tick()
    assert e.state == G.VERIFIED and not w.records           # 기록 지우고 되돌림
    assert any("다시 올림" in h for h in e.history)
    w.t += 5 * 60
    g.tick()
    assert e.state == G.CONFIRMED and w.calls.count(f"upload {STEM}.mkv") == 2


def test_unknown_remote_is_not_a_failure(w):
    w.remote_unknown = True
    g = w.guard()
    g.tick()
    e = _e(g)
    assert e.state == G.UPLOADED and e.attempts == 0 and e.next_try == w.t + 15 * 60
    w.remote_unknown = False
    w.t += 15 * 60
    g.tick()
    assert e.state == G.CONFIRMED


def test_blocked_is_kept_and_rechecked_hourly(w):
    w.upload_error = G.Blocked("public 저장소")
    g = w.guard()
    g.tick()
    e = _e(g)
    assert e.state == G.BLOCKED and e.attempts == 0 and "public" in e.last_error
    w.upload_error = None
    w.t += 30 * 60
    g.tick()
    assert e.state == G.BLOCKED
    w.t += 31 * 60
    g.tick()
    assert e.state == G.CONFIRMED


def test_upload_failure_backs_off(w):
    w.upload_error = OSError("네트워크 끊김")
    g = w.guard()
    g.tick()
    e = _e(g)
    assert e.state == G.VERIFIED and e.attempts == 1 and "네트워크" in e.last_error


def test_old_silence_hold_marker_no_longer_blocks_upload(w):
    """사양 변경(사용자 요청 '무음 감지 시 업로드 차단 금지'): 예전 '무음 보류' 표시가 있어도 올림."""
    G.mark_held(_f(w), "장치 무음")
    g = w.guard()
    g.tick()
    assert _e(g).state == G.CONFIRMED and f"upload {STEM}.mkv" in w.calls


def test_entry_left_held_by_an_old_ledger_is_uploaded(w):
    g = w.guard()
    g.ledger.entries[str(_f(w))] = G.Entry(str(_f(w)), state=G.HELD,
                                           size=_f(w).stat().st_size, mtime=_f(w).stat().st_mtime)
    g.tick()
    e = _e(g)
    assert e.state == G.CONFIRMED and any("보류 해제" in h for h in e.history)


def test_lost_ledger_is_rebuilt_without_uploading_twice(w):
    g = w.guard()
    g.tick()
    (w.root.parent / "ledger.json").unlink()
    w.calls.clear()
    g2 = w.guard()
    g2.tick()
    assert _e(g2).state == G.CONFIRMED and not any(c.startswith("upload") for c in w.calls)


def test_changed_file_starts_over(w):
    g = w.guard()
    g.tick()
    _f(w).write_bytes(b"mkv-data-longer")
    g.tick()
    assert _e(g).state == G.CONFIRMED and w.calls.count(f"upload {STEM}.mkv") == 2


def test_deleted_file_leaves_the_ledger(w):
    g = w.guard()
    g.tick()
    _f(w).unlink()
    g.tick()
    assert g.ledger.entries == {}


def test_media_files_skips_work_audio_leftover_and_temp(tmp_path):
    root = tmp_path / "r"
    keep = root / "d" / "s" / f"{STEM}.mkv"
    for p in (keep, root / "d" / "s" / ".work" / "x.mkv", root / "d" / "s" / "audio" / "a.mp4",
              root / "d" / "s" / ".work" / "leftover" / "b.mkv",
              root / "d" / "s" / f"{STEM}.rebuild.mkv"):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert G.media_files(root) == [keep]


def test_status_text_lists_what_is_not_done(w):
    w.upload_error = OSError("네트워크 끊김")
    g = w.guard()
    g.tick()
    text = G.status_text(g.ledger)
    assert "검사 통과 1" in text and STEM in text and "네트워크" in text


# -- 다시 만들기 ----------------------------------------------------------------
def test_rebuild_keeps_the_original_for_rollback(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    f = tmp_path / "s" / f"{STEM}.mkv"
    f.parent.mkdir()
    f.write_bytes(b"broken")
    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")

    def fake_run(cmd, **_k):
        Path(cmd[-1]).write_bytes(b"remuxed")
        return types.SimpleNamespace(returncode=0, stderr=b"")
    monkeypatch.setattr(G.subprocess, "run", fake_run)
    how = G.rebuild_media(f, verify=lambda p: None)
    assert "remux" in how and f.read_bytes() == b"remuxed"
    [bad] = list((f.parent / ".work" / "leftover").iterdir())
    assert bad.read_bytes() == b"broken" and bad.name.startswith(f"{STEM}.mkv.bad-")


def test_rebuild_falls_back_to_sound_only(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    f = tmp_path / "s" / f"{STEM}.mkv"
    (f.parent / "audio").mkdir(parents=True)
    f.write_bytes(b"broken")
    (f.parent / "audio" / f"{STEM}.system.wav").write_bytes(b"R" * 9000)
    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(G.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b"Invalid data"))
    muxed = []

    def mux(out, video, vstart, inputs, **kw):
        muxed.append([i.name for i in inputs])
        Path(out).write_bytes(b"sound-only")
    assert "소리만" in G.rebuild_media(f, verify=lambda p: None, mux=mux)
    assert muxed == [["system"]] and f.read_bytes() == b"sound-only"


def test_rebuild_without_material_raises_and_keeps_file(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    f = tmp_path / f"{STEM}.mkv"
    f.write_bytes(b"broken")
    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(G.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b""))
    with pytest.raises(RuntimeError):
        G.rebuild_media(f, verify=lambda p: None)
    assert f.read_bytes() == b"broken"


# -- CLI · 설정 -----------------------------------------------------------------
def _cfg(extra=None):
    return C.Config(data=C._deep_merge(C.DEFAULTS, extra or {}))


class _Up:
    slug = "o/r"

    def __init__(self, public):
        self.public, self.uploads = public, []

    def is_public(self):
        return self.public

    def upload(self, path, tag, prefix=""):
        self.uploads.append(tag)
        return {"assets": [{"name": Path(path).name}]}


def test_public_block_is_off_by_default(tmp_path):
    from syncnote import cli

    cli._PUBLIC_CACHE.clear()
    up = _Up(public=True)
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    cli._upload_recording_file(_cfg(), cli._store(Layout(tmp_path), "recordings"), up, f, "t")
    assert up.uploads == ["t"]


def test_public_block_when_switched_on(tmp_path):
    from syncnote import cli

    cli._PUBLIC_CACHE.clear()
    up = _Up(public=True)
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    with pytest.raises(cli.PublicRepoBlocked):
        cli._upload_recording_file(_cfg({"recording": {"upload": {"block_public": True}}}),
                                   cli._store(Layout(tmp_path), "recordings"), up, f, "t")
    assert up.uploads == []


def test_example_and_defaults_agree_on_new_settings():
    # 3.10 이면 tomli — config 가 고른 것 그대로
    path = Path(__file__).resolve().parents[1] / "config.example.toml"
    ex = C.tomllib.loads(path.read_text(encoding="utf-8"))
    assert ex["recording"]["upload"] == C.DEFAULTS["recording"]["upload"]
    assert ex["recording"]["guard"] == C.DEFAULTS["recording"]["guard"]


def test_file_origin_from_outbox_or_path(tmp_path):
    from datetime import date

    from syncnote import cli

    layout = Layout(tmp_path)
    f = tmp_path / "recordings" / "2026-11-23" / "자료구조" / f"{STEM}.mkv"
    assert cli._file_origin(layout, f) == ("", "자료구조", date(2026, 11, 23))
    outbox.add(layout.state, f, course_id="C1", label="수업", day="2026-11-20")
    assert cli._file_origin(layout, f) == ("C1", "수업", date(2026, 11, 20))


def test_cmd_guard_status_and_switch(tmp_path, monkeypatch, capsys):
    from syncnote import cli

    monkeypatch.setattr(cli, "_load", lambda _a: (_cfg({"recording": {"guard": {
        "enabled": False}}}), Layout(tmp_path)))
    args = types.SimpleNamespace(status=False, once=True, dry_run=False)
    assert cli.cmd_record_guard(args) == 0
    assert "꺼져 있습니다" in capsys.readouterr().out
    args.status = True
    assert cli.cmd_record_guard(args) == 0
    assert "녹화 지킴이 상태" in capsys.readouterr().out


def test_cmd_guard_once_runs_a_check(tmp_path, monkeypatch, capsys):
    from syncnote import cli

    w = World(tmp_path / "recordings")
    f = w.root / "2026-11-23" / "s" / f"{STEM}.mkv"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    monkeypatch.setattr(cli, "_load", lambda _a: (_cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_guard_hooks", lambda *a, **k: w.hooks())
    rc = cli.cmd_record_guard(types.SimpleNamespace(status=False, once=True, dry_run=False))
    assert rc == 0 and "확인됨 1" in capsys.readouterr().out
    led = json.loads((tmp_path / "state" / G.LEDGER).read_text(encoding="utf-8"))
    assert list(led["entries"].values())[0]["state"] == G.CONFIRMED


# -- 심각도: 내용을 잃지 않는 쪽 -------------------------------------------------
def _leveled(w, level):
    hooks = w.hooks()
    first = {"done": False}

    def verify(p):
        if not first["done"]:
            first["done"] = True
            return G.Problem("망가짐", level)
        return None
    hooks.verify = verify
    return G.Guard(w.root, G.Ledger(w.root.parent / "ledger.json"), hooks, now=lambda: w.t)


def test_broken_frames_are_uploaded_as_is(w):
    g = _leveled(w, G.FRAMES)
    g.tick()
    e = _e(g)
    assert e.state == G.CONFIRMED and not any(c.startswith("rebuild") for c in w.calls)
    assert any("그대로 올림" in h for h in e.history)


def test_structure_damage_is_remuxed_but_never_turned_into_sound_only(w):
    g = _leveled(w, G.STRUCTURE)
    g.tick()
    assert f"rebuild {STEM}.mkv sound_only=False" in w.calls and _e(g).state == G.CONFIRMED


def test_structure_damage_that_cannot_be_fixed_still_uploads_the_original(w):
    w.rebuild_ok = False
    g = _leveled(w, G.STRUCTURE)
    g.tick()
    e = _e(g)
    assert e.state == G.CONFIRMED and e.attempts == 0
    assert any("원본 그대로 올림" in h for h in e.history)


def test_rebuild_without_sound_only_raises_when_remux_fails(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    f = tmp_path / "s" / f"{STEM}.mkv"
    (f.parent / "audio").mkdir(parents=True)
    f.write_bytes(b"broken")
    (f.parent / "audio" / f"{STEM}.system.wav").write_bytes(b"R" * 9000)
    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(G.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b""))
    with pytest.raises(RuntimeError):
        G.rebuild_media(f, False, verify=lambda p: None, mux=lambda *a, **k: None)
    assert f.read_bytes() == b"broken"                       # 화면 파일을 소리만으로 바꾸지 않음


@pytest.mark.parametrize("stderr, rc, level", [
    (b"", 0, None),
    (b"[h264 @ 0x1] error while decoding MB 7 2, bytestream -29\n", 0, G.FRAMES),
    (b"[in#0/matroska,webm @ 0x1] 0x00 at pos 3 invalid as first byte of an EBML number\n"
     b"[h264 @ 0x1] error while decoding MB 7 2\n", 0, G.STRUCTURE),
    (b"Invalid data found when processing input\n", 1, G.FATAL),
])
def test_verify_media_levels(tmp_path, monkeypatch, stderr, rc, level):
    from syncnote.recorder import ffmpeg_util as fu

    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "duration_of", lambda p: 30.0)
    monkeypatch.setattr(G.subprocess, "run",
                        lambda cmd, **k: types.SimpleNamespace(returncode=rc, stderr=stderr))
    got = G.verify_media(tmp_path / "a.mkv")
    assert (got.level if got else None) == level


def test_verify_media_no_length_is_fatal(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "duration_of", lambda p: None)
    assert G.verify_media(tmp_path / "a.mkv").level == G.FATAL
