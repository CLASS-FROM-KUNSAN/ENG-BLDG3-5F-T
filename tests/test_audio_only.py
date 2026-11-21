"""ffmpeg 가 없을 때 소리만 녹음하고, 나중에 파일로 만들기(record remux)."""

from __future__ import annotations

import json
import struct
import time
import types
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from syncnote.paths import Layout
from syncnote.recorder import remux as R
from syncnote.recorder.meta import ClassMeta, RecordingMeta
from syncnote.recorder.session import RecordingSession, SessionResult


class Cfg:
    machine_label = "PC"

    def __init__(self, d=None):
        self._d = d or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def section(self, _k):
        return {}


def _wav(path: Path, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * int(8000 * seconds))
    return path


def _meta():
    t0 = datetime(2026, 11, 23, 9, 0)
    return RecordingMeta("자료구조", "schedule",
                         [ClassMeta("자료구조", t0, t0 + timedelta(minutes=75), course_id="C1",
                                    room="401호")],
                         window_start=t0 - timedelta(minutes=2),
                         window_end=t0 + timedelta(minutes=78), pre_roll_min=2,
                         post_roll_min=3, machine="PC", version="0.1.0")


def test_meta_survives_save_and_restore():
    m = _meta()
    back = RecordingMeta.from_state(json.loads(json.dumps(m.to_state())))
    assert back == m


# -- 녹음: ffmpeg 를 처음에 한 번 보고 없으면 화면을 끔 ---------------------------
def test_run_switches_to_audio_only_once_when_ffmpeg_is_missing(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    def missing():
        raise fu.FFmpegMissing()
    monkeypatch.setattr(fu, "find_ffmpeg", missing)
    s = RecordingSession(Cfg({"recording.segment_minutes": 0.06}), Layout(tmp_path), "수업")
    seen = []
    monkeypatch.setattr(s, "_record_segment",
                        lambda i, d, m, c, video_on, audio_on, r: (seen.append(video_on),
                                                                  s.request_stop())[1])
    r = s.run(duration_minutes=0.13)
    assert s.audio_only and seen == [False]
    assert sum("소리만 녹음" in w for w in r.warnings) == 1                # 경고는 한 번만


def test_run_keeps_video_when_ffmpeg_is_there(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as fu

    monkeypatch.setattr(fu, "find_ffmpeg", lambda: "ffmpeg")
    s = RecordingSession(Cfg({"recording.segment_minutes": 0.06}), Layout(tmp_path), "수업")
    seen = []
    monkeypatch.setattr(s, "_record_segment",
                        lambda i, d, m, c, video_on, audio_on, r: (seen.append(video_on),
                                                                  s.request_stop())[1])
    s.run(duration_minutes=0.07)
    assert not s.audio_only and seen == [True]


def _audio_only_segment(tmp_path, cfg_extra=None):
    from syncnote.recorder import ffmpeg_util as fu
    from syncnote.recorder import session as sess_mod

    out = tmp_path / "rec"
    s = RecordingSession(Cfg({"transcribe.enabled": False, "recording.audio.mic_enabled": False,
                              **(cfg_extra or {})}),
                         Layout(tmp_path), "자료구조", meta=_meta())
    s.audio_only = True
    started = time.time()
    track = types.SimpleNamespace(name="system", path=_wav(out / ".work" / "sys.wav"),
                                  started_at=started, error=None, silent=False)
    ar = MagicMock()
    ar.stop.return_value = [track]
    ar.started_at = started
    with patch.object(sess_mod, "AudioRecorder", return_value=ar), \
         patch.object(sess_mod, "mux") as mux, \
         patch.object(fu, "duration_of", return_value=None):
        seg = s._record_segment(1, out, 0.001, "mkv", False, True,
                                SessionResult("자료구조", datetime.now()))
    return out, seg, mux


def test_audio_only_segment_keeps_wav_and_leaves_a_pending_note(tmp_path):
    out, seg, mux = _audio_only_segment(tmp_path)
    assert not mux.called                                              # 합치지 않음
    wavs = list((out / "audio").glob("*.wav"))
    assert len(wavs) == 1                                              # 전사 꺼져 있어도 남김
    pend = R.find_pending(out)
    assert len(pend) == 1
    d = json.loads(pend[0].read_text(encoding="utf-8"))
    assert d["final"] == str(seg.path) and not seg.path.exists()       # 올릴 파일 없음 -> 업로드 안 됨
    assert [t["name"] for t in d["tracks"]] == ["system"]
    assert Path(d["tracks"][0]["path"]) == wavs[0]
    assert d["delete_wav"] is True and d["meta"]["label"] == "자료구조"


# -- 나중에 파일 만들기 --------------------------------------------------------
def _pending(tmp_path, *, delete_wav=False):
    wav = _wav(tmp_path / "audio" / "a.wav")
    final = tmp_path / "20261123_090000_part001.mkv"
    p = R.write_pending(final, [("system", wav, 1000.0)], layout="separate", meta=_meta(),
                        segment_index=2, ended_at=1060.0, delete_wav=delete_wav,
                        course_id="C1", started=datetime(2026, 11, 23, 9, 0))
    return p, final, wav


def test_remux_one_builds_the_file_with_metadata_and_removes_the_note(tmp_path):
    p, final, wav = _pending(tmp_path, delete_wav=True)
    calls = []

    def fake_mux(out, video, vstart, inputs, **kw):
        calls.append((out, video, [i.name for i in inputs], kw))
        Path(out).write_bytes(b"mkv")
    r = R.remux_one(p, mux=fake_mux)
    out, video, names, kw = calls[0]
    assert out == final and video is None and names == ["system"]
    assert kw["meta"] == _meta() and kw["segment_index"] == 2 and kw["ended_at"] == 1060.0
    assert kw["delete_audio"] is True and kw["layout"] == "separate"
    assert not p.exists() and r.course_id == "C1" and r.seconds == 60.0
    assert r.started == datetime(2026, 11, 23, 9, 0)


def test_remux_failure_keeps_the_note_for_next_time(tmp_path):
    p, _final, wav = _pending(tmp_path)
    wav.unlink()
    done, failed = R.remux_all(tmp_path, mux=lambda *a, **k: None, say=lambda _m: None)
    assert done == [] and len(failed) == 1 and "WAV" in failed[0][1] and p.exists()

    p2, _f2, _w2 = _pending(tmp_path / "other")

    def boom(*_a, **_k):
        raise RuntimeError("ffmpeg 실패")
    _done, failed = R.remux_all(tmp_path / "other", mux=boom, say=lambda _m: None)
    assert failed and p2.exists()


def test_cli_remux_lists_and_needs_ffmpeg(tmp_path, monkeypatch, capsys):
    from syncnote import cli
    from syncnote.recorder import ffmpeg_util as fu

    layout = Layout(tmp_path)
    _pending(layout.recordings / "2026-11-23" / "자료구조")
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), layout))
    args = types.SimpleNamespace(dry_run=True, upload=False, upload_dry_run=False)
    assert cli.cmd_record_remux(args) == 0
    assert "1개" in capsys.readouterr().out

    def missing():
        raise fu.FFmpegMissing()
    monkeypatch.setattr(fu, "find_ffmpeg", missing)
    args.dry_run = False
    assert cli.cmd_record_remux(args) == 1
    assert R.find_pending(layout.recordings)                           # 아무것도 안 지움


def test_real_ffmpeg_remux_puts_tags_and_chapters_in(tmp_path):
    import subprocess

    from syncnote.recorder.ffmpeg_util import find_ffmpeg, find_ffprobe

    try:
        find_ffmpeg()
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg 없음")
    probe = find_ffprobe()
    if not probe:
        pytest.skip("ffprobe 없음")
    base = time.time()
    t0 = datetime.fromtimestamp(base)
    meta = RecordingMeta("자료구조", "schedule",
                         [ClassMeta("자료구조", t0 + timedelta(seconds=2), t0 + timedelta(seconds=4))],
                         window_start=t0, window_end=t0 + timedelta(seconds=6))
    wav = _wav(tmp_path / "audio" / "a.wav", seconds=6)
    final = tmp_path / "out.mkv"
    p = R.write_pending(final, [("system", wav, base)], layout="separate", meta=meta,
                        segment_index=1, ended_at=base + 6)
    R.remux_one(p)
    j = json.loads(subprocess.run([probe, "-v", "error", "-show_chapters", "-show_format",
                                   "-of", "json", str(final)], capture_output=True, text=True,
                                  encoding="utf-8").stdout)
    assert [c["tags"]["title"] for c in j["chapters"]] == ["수업 전 여유", "자료구조", "수업 후 여유"]
    assert wav.exists()                                                # delete_wav=False
