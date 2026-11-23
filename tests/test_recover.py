"""녹화 도중 컴퓨터가 꺼졌을 때: 다음 시작에 남은 조각을 파일로 만들어 올리기."""

from __future__ import annotations

import json
import struct
import time
import types
import wave
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from syncnote import config as C
from syncnote.paths import Layout
from syncnote.recorder import outbox
from syncnote.recorder import recover as RV

STEM = "20261123_091500_part002"


def _cfg(extra=None):
    return C.Config(data=C._deep_merge(C.DEFAULTS, extra or {}))


def _wav(path: Path, seconds: float = 2.0, *, break_header: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 100) * int(8000 * seconds))
    if break_header:                       # 전원이 꺼지면 길이 칸이 0 으로 남음
        with path.open("r+b") as fh:
            fh.seek(4)
            fh.write(struct.pack("<I", 0))
            fh.seek(40)
            fh.write(struct.pack("<I", 0))
    return path


def _crashed(tmp_path, *, journal=True, video=True, monitors=("",)):
    work = tmp_path / "recordings" / "2026-11-23" / "자료구조" / ".work"
    work.mkdir(parents=True)
    for sfx in monitors if video else ():
        (work / f"{STEM}{sfx}.video.mkv").write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 5000)
    _wav(work / f"{STEM}.system.wav", break_header=True)
    _wav(work / f"{STEM}.mic.wav")
    if journal:
        (work / f"{STEM}.journal.json").write_text(json.dumps({
            "version": 1, "stem": STEM, "index": 2, "container": "mkv", "layout": "separate",
            "label": "자료구조", "course_id": "C1", "started": "2026-11-23T09:15:00",
            "audio": {"system": 1000.5, "mic": 1000.7}, "video": {"": 1001.0}, "meta": None},
            ensure_ascii=False), encoding="utf-8")
    return work


def _fake_mux(calls, fail_video=False):
    def mux(out, video, vstart, inputs, **kw):
        calls.append(types.SimpleNamespace(out=Path(out), video=video, vstart=vstart,
                                           inputs={i.name: i.started_at for i in inputs}, kw=kw))
        if fail_video and video is not None:
            raise RuntimeError("ffmpeg 실패\nEBML header parsing failed\nInvalid data found")
        Path(out).write_bytes(b"final")
        if video is not None and kw.get("delete_video"):
            Path(video).unlink()
        return Path(out)
    return mux


# -- WAV 머리 ---------------------------------------------------------------
def test_repair_wav_fixes_zeroed_lengths(tmp_path):
    p = _wav(tmp_path / "a.wav", 1.5, break_header=True)
    assert RV.repair_wav(p) is True
    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 12000
    assert RV.repair_wav(p) is False                     # 두 번째는 고칠 것 없음


def test_repair_wav_drops_a_half_written_last_sample(tmp_path):
    p = _wav(tmp_path / "a.wav", 1.0)
    with p.open("ab") as fh:
        fh.write(b"\x01")                               # 반쪽 표본
    RV.repair_wav(p)
    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 8000


# -- 찾기 ------------------------------------------------------------------
def test_find_leftovers_groups_pieces_by_stem(tmp_path):
    _crashed(tmp_path, monitors=("_mon1", "_mon2"))
    [lo] = RV.find_leftovers(tmp_path / "recordings")
    assert lo.stem == STEM and sorted(s for _p, s in lo.videos) == ["_mon1", "_mon2"]
    assert set(lo.wavs) == {"system", "mic"} and lo.journal["course_id"] == "C1"
    assert lo.started == datetime(2026, 11, 23, 9, 15)


def test_finished_session_leaves_nothing_to_find(tmp_path):
    (tmp_path / "recordings" / "d" / "s" / ".work" / RV.LEFTOVER_DIR).mkdir(parents=True)
    (tmp_path / "recordings" / "d" / "s" / f"{STEM}.mkv").write_bytes(b"x")
    assert RV.find_leftovers(tmp_path / "recordings") == []


# -- 합치기 ----------------------------------------------------------------
def test_finalize_muxes_with_journal_times_and_cleans_up(tmp_path):
    work = _crashed(tmp_path)
    [lo] = RV.find_leftovers(tmp_path / "recordings")
    calls = []
    rec = RV.finalize(lo, _cfg(), mux=_fake_mux(calls), ffmpeg_ok=True)
    [c] = calls
    assert c.out == work.parent / f"{STEM}.mkv" and c.video is not None and c.vstart == 1001.0
    assert c.inputs == {"system": 1000.5, "mic": 1000.7}
    assert c.kw["segment_index"] == 2 and c.kw["delete_video"] is True
    assert rec.finals == [c.out] and not rec.audio_only
    assert not work.exists()                                       # 작업 폴더 정리됨
    assert sorted(p.name for p in (work.parent / "audio").iterdir()) == [
        f"{STEM}.mic.wav", f"{STEM}.system.wav"]                   # 전사용으로 남김
    seg = rec.segment()
    assert seg.index == 2 and seg.started_at == datetime(2026, 11, 23, 9, 15)


def test_broken_video_still_saves_the_sound(tmp_path):
    work = _crashed(tmp_path)
    [lo] = RV.find_leftovers(tmp_path / "recordings")
    calls = []
    rec = RV.finalize(lo, _cfg(), mux=_fake_mux(calls, fail_video=True), ffmpeg_ok=True)
    assert [c.video is None for c in calls] == [False, True]
    assert rec.audio_only and rec.finals
    assert any("Invalid data found" in n and "\n" not in n for n in rec.notes)
    assert (work / RV.LEFTOVER_DIR / f"{STEM}.video.mkv").exists()   # 다음 시작 때 또 안 붙잡음
    assert RV.find_leftovers(tmp_path / "recordings") == []


def test_without_journal_pieces_are_still_joined(tmp_path):
    _crashed(tmp_path, journal=False)
    [lo] = RV.find_leftovers(tmp_path / "recordings")
    calls = []
    rec = RV.finalize(lo, _cfg(), mux=_fake_mux(calls), ffmpeg_ok=True)
    assert rec.finals and calls[0].inputs == {"system": 0.0, "mic": 0.0}
    assert rec.segment().started_at == datetime(2026, 11, 23, 9, 15)   # stem 에서


def test_no_ffmpeg_defers_to_record_remux(tmp_path):
    work = _crashed(tmp_path, video=False)
    [lo] = RV.find_leftovers(tmp_path / "recordings")
    rec = RV.finalize(lo, _cfg(), mux=_fake_mux([]), ffmpeg_ok=False)
    assert rec.pending is not None and rec.pending.exists() and not rec.finals
    assert not (work / f"{STEM}.journal.json").exists()


def test_busy_recorder_pieces_are_left_alone(tmp_path):
    _crashed(tmp_path)
    assert RV.recover_all(tmp_path / "recordings", _cfg(), busy=True, mux=_fake_mux([]),
                          ffmpeg_ok=True, now=time.time()) == []
    assert len(RV.recover_all(tmp_path / "recordings", _cfg(), busy=True, mux=_fake_mux([]),
                              ffmpeg_ok=True, now=time.time() + 999)) == 1


# -- 올릴 목록 ---------------------------------------------------------------
def test_outbox_add_is_idempotent_and_remove_works(tmp_path):
    outbox.add(tmp_path, tmp_path / "a.mkv", label="x", day="2026-11-23")
    outbox.add(tmp_path, tmp_path / "a.mkv", label="x", day="2026-11-23")
    outbox.add(tmp_path, tmp_path / "b.mkv")
    assert [Path(i["path"]).name for i in outbox.items(tmp_path)] == ["a.mkv", "b.mkv"]
    outbox.remove(tmp_path, tmp_path / "a.mkv")
    assert [Path(i["path"]).name for i in outbox.items(tmp_path)] == ["b.mkv"]


class _Uploader:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def upload(self, path, tag, prefix=""):
        self.calls.append((Path(path).name, tag))
        if self.fail:
            raise OSError("네트워크 끊김")
        return {"assets": [{"name": Path(path).name}]}


def _segment(path: Path, day=datetime(2026, 11, 20, 9, 0)):
    from syncnote.recorder.session import Segment

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return Segment(1, path, day, 60.0)


def test_failed_upload_stays_listed_and_is_retried_later(tmp_path, monkeypatch):
    from syncnote import cli

    layout = Layout(tmp_path)
    cfg = _cfg({"github": {"owner": "o", "repo": "r"}})
    up = _Uploader(fail=True)
    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: up)
    monkeypatch.setattr("syncnote.transcribe.batch.enqueue_segment", lambda *a, **k: None)
    seg = _segment(tmp_path / "recordings" / "2026-11-20" / "수업" / f"{STEM}.mkv")
    on_done = cli._post_segment_factory(cfg, layout, cli._store(layout, "recordings"), "수업",
                                        upload=True, dry_run=False)
    on_done(seg)
    assert up.calls == [(seg.path.name, "recordings-2026-11-20")]   # 녹화한 날 릴리스
    assert [Path(i["path"]) for i in outbox.items(layout.state)] == [seg.path]

    up.fail = False
    assert cli._drain_outbox(cfg, layout) == (1, 0)
    assert up.calls[-1] == (seg.path.name, "recordings-2026-11-20")
    assert outbox.items(layout.state) == []
    assert cli._drain_outbox(cfg, layout) == (0, 0)


def test_successful_upload_is_not_left_in_the_list(tmp_path, monkeypatch):
    from syncnote import cli

    layout = Layout(tmp_path)
    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: _Uploader())
    monkeypatch.setattr("syncnote.transcribe.batch.enqueue_segment", lambda *a, **k: None)
    on_done = cli._post_segment_factory(_cfg(), layout, cli._store(layout, "recordings"), "수업",
                                        upload=True, dry_run=False)
    on_done(_segment(tmp_path / "x" / "a.mkv"))
    assert outbox.items(layout.state) == []


def test_recover_recordings_end_to_end(tmp_path, monkeypatch):
    """꺼진 녹화 → 다음 시작: 파일을 만들고, 전사 대기열에 넣고, 녹화한 날 릴리스로 올림."""
    from syncnote import cli
    from syncnote.recorder import ffmpeg_util as fu
    from syncnote.recorder import mux as mux_mod

    _crashed(tmp_path)
    layout = Layout(tmp_path)
    up = _Uploader()
    queued = []
    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: up)
    monkeypatch.setattr("syncnote.transcribe.batch.enqueue_segment",
                        lambda cfg, lay, seg, label, **k: queued.append((seg.index, label)))
    monkeypatch.setattr(mux_mod, "mux", _fake_mux([]))
    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "duration_of", lambda _p: 1800.0)
    assert cli._recover_recordings(_cfg(), layout, upload=True, dry_run=False) == 1
    assert queued == [(2, "자료구조")]
    assert up.calls == [(f"{STEM}.mkv", "recordings-2026-11-23")]
    assert outbox.items(layout.state) == []
    assert cli._recover_recordings(_cfg(), layout, upload=True, dry_run=False) == 0  # 두 번 안 함


def _starts(tmp_path, monkeypatch, cfg_extra):
    from syncnote import cli

    seen = []
    monkeypatch.setattr(cli, "_recover_in_background", lambda *a, **k: seen.append("뒤에서"))
    monkeypatch.setattr(cli, "_recover_recordings", lambda *a, **k: seen.append("그 자리에서"))
    cfg = _cfg(cfg_extra)
    monkeypatch.setattr(cli, "_load", lambda _a: (cfg, Layout(tmp_path)))
    args = types.SimpleNamespace(show=False, once=True, no_upload=False, dry_run=False,
                                 label=None, course="", language=None)
    return cli, args, seen


def test_schedule_with_no_timetable_still_recovers_before_exiting(tmp_path, monkeypatch):
    cli, args, seen = _starts(tmp_path, monkeypatch, {"recording": {"timetable": []}})
    assert cli.cmd_record_schedule(args) == 1
    assert seen == ["그 자리에서"]          # 곧 끝나는 프로그램 — 뒤에서 돌리면 함께 끊김


def test_switched_off_triggers_still_finish_old_recordings(tmp_path, monkeypatch):
    cli, args, seen = _starts(tmp_path, monkeypatch, {"recording": {"triggers": {
        "schedule": False, "watch": False}}})
    assert cli.cmd_record_schedule(args) == 0 and cli.cmd_record_watch(args) == 0
    assert seen == ["그 자리에서", "그 자리에서"]


def test_waiting_schedule_and_watch_recover_in_background(tmp_path, monkeypatch):
    from syncnote.recorder import schedule as sched

    cli, args, seen = _starts(tmp_path, monkeypatch, {"recording": {"timetable": [
        {"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:00"}]}})
    monkeypatch.setattr(sched, "wait_until", lambda _t: False)        # 대기 중 종료
    assert cli.cmd_record_schedule(args) == 0
    with patch("syncnote.recorder.watcher.Watcher") as W:
        W.return_value.run = MagicMock(side_effect=KeyboardInterrupt)
        W.return_value.loop = MagicMock(side_effect=KeyboardInterrupt)
        try:
            cli.cmd_record_watch(args)
        except BaseException:
            pass
    assert seen == ["뒤에서", "뒤에서"]


def test_cli_recover_lists_what_is_left(tmp_path, monkeypatch, capsys):
    from syncnote import cli

    _crashed(tmp_path)
    outbox.add(Layout(tmp_path).state, tmp_path / "old.mkv", day=date(2026, 11, 1).isoformat())
    monkeypatch.setattr(cli, "_load", lambda _a: (_cfg(), Layout(tmp_path)))
    rc = cli.cmd_record_recover(types.SimpleNamespace(list=True, no_upload=False, dry_run=False))
    out = capsys.readouterr().out
    assert rc == 0 and "끝나지 않은 녹화 조각 1개" in out and "올릴 목록에 남은 파일 1개" in out
    assert "일지 있음" in out


def test_segment_writes_a_journal_while_recording_and_clears_it_after(tmp_path):
    """정상으로 끝난 세그먼트는 일지가 남지 않아야 다음 시작 때 '꺼진 녹화' 로 안 봅니다."""
    from syncnote.recorder import ffmpeg_util as fu
    from syncnote.recorder import session as sess_mod
    from syncnote.recorder.session import RecordingSession, SessionResult

    out = tmp_path / "rec"
    # 마이크를 끄지 않으면 마이크를 찾느라 soundcard 를 부름(CI 에는 없음) — test_audio_only 와 같게
    s = RecordingSession(_cfg({"transcribe": {"enabled": False},
                               "recording": {"audio": {"mic_enabled": False}}}),
                         Layout(tmp_path), "자료구조")
    s.audio_only = True
    seen = []
    real_write = RV.write_journal

    def spy(work, stem, data):
        seen.append(dict(data))
        real_write(work, stem, data)
    track = types.SimpleNamespace(name="system", path=_wav(out / ".work" / "x.system.wav"),
                                  started_at=time.time(), error=None, silent=False)
    ar = MagicMock()
    ar.stop.return_value = [track]
    ar.started_at = time.time()
    with patch.object(sess_mod, "AudioRecorder", return_value=ar), \
         patch.object(RV, "write_journal", spy), \
         patch.object(fu, "duration_of", return_value=None):
        s._record_segment(3, out, 0.001, "mkv", False, True, SessionResult("자료구조", datetime.now()))
    assert seen and seen[0]["index"] == 3 and seen[0]["label"] == "자료구조"
    assert seen[0]["container"] == "mkv" and seen[0]["stem"].endswith("_part003")
    assert list(out.rglob("*" + RV.JOURNAL)) == []
