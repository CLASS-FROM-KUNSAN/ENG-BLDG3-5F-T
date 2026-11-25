"""녹화를 막는 구조를 두지 않음(사용자 요청: 녹화를 막는 구조와 알고리즘을 찾아 고칠 것).

- 세그먼트 하나가 실패해도 녹화 전체를 멈추지 않음(간격을 두고 다음 세그먼트).
- 화면 ffmpeg 가 죽어도 그 세그먼트만 마무리하고 계속. 연달아 곧바로 죽으면 그 세그먼트는 소리만.
- 무음 시간은 세그먼트를 넘어 이어서 셈(30분 세그먼트에서 60분 경고가 나오도록).
- 시간표 녹화가 시작조차 못 하면 수업이 끝나기 전까지 간격을 늘려 다시 시도.
(감시 녹화는 tests/test_watcher.py)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from syncnote import cli
from syncnote.paths import Layout
from syncnote.recorder.audio import TrackResult
from syncnote.recorder.session import RecordingSession


class Cfg:
    def __init__(self, data=None):
        self._d = data or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


class FakeRecorder:
    def __init__(self, tracks):
        self.results = tracks


def track(name, silent_for):
    return TrackResult(name=name, path=Path(f"/tmp/{name}.wav"), silent_for=silent_for)


class _NoWaitEvent(threading.Event):
    """백오프 대기를 기록만 하고 바로 돌아옵니다(테스트가 수십 초 걸리지 않게)."""

    def __init__(self):
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.is_set()


def session(tmp_path, **cfg):
    data = {"recording.audio.enabled": False, "recording.video.enabled": False,
            "recording.audio.silence_stop_sec": 900, "recording.audio.silence_abort_sec": 3600}
    data.update(cfg)
    s = RecordingSession(Cfg(data), Layout(tmp_path), "수업", stop_on_silence=True)
    s._stop_event = _NoWaitEvent()
    return s


# --- (A) 세그먼트 실패 ---------------------------------------------------------

def test_a_failed_segment_does_not_end_the_recording(tmp_path):
    """예전: 세그먼트 하나가 예외를 던지면 break — 수업의 나머지가 통째로 빔."""
    s = session(tmp_path)
    calls = []

    def seg(index, *a, **kw):
        calls.append(index)
        if len(calls) <= 2:
            raise RuntimeError("장치가 잠깐 사라짐")
        s._stop_event.set()
        return

    s._record_segment = seg
    result = s.run(duration_minutes=10)
    assert calls == [1, 2, 3], "실패한 세그먼트 뒤에 녹화를 이어 가지 않았습니다"
    assert s._stop_event.waits[:2] == [5, 15], "다시 시도 간격이 늘어나지 않습니다"
    assert any("녹화는 계속" in w for w in result.warnings)


# --- (B) 화면 ffmpeg 가 죽음 ---------------------------------------------------

def test_video_death_ends_only_the_segment_and_falls_back_to_audio(tmp_path):
    """예전: 화면 ffmpeg 가 죽으면 _stop_event.set() — 녹화 전체가 끝남."""
    s = session(tmp_path, **{"recording.video.enabled": True})
    video_flags = []

    def seg(index, out_dir, length, container, video_on, audio_on, result):
        video_flags.append(video_on)
        if video_on:
            s._video_died = True          # 곧바로 죽음
        if len(video_flags) >= 5:
            s._stop_event.set()
        return

    s._record_segment = seg
    s.run(duration_minutes=10)
    # 두 번 연달아 곧바로 죽으면 한 세그먼트는 소리만, 그다음 화면을 다시 시도
    assert video_flags == [True, True, False, True, True]


# --- 무음 시간 이어서 세기 -----------------------------------------------------

def test_silence_carries_across_segments(tmp_path):
    """예전: 트랙(세그먼트)마다 0 부터 세서 30분 세그먼트에서 60분 경고가 나오지 않음."""
    s = session(tmp_path)
    s._segment_t0 = time.time() - 1800
    s._carry_silence(FakeRecorder([track("system", 1800.0)]), time.time())
    s._segment_t0 = time.time() - 1800
    s._carry_silence(FakeRecorder([track("system", 1800.0)]), time.time())
    assert s._quiet_carry >= 3600 - 1

    s._segment_t0 = time.time() - 60
    verdict = s._silence_verdict(FakeRecorder([track("system", 60.0)]))
    assert verdict is not None and verdict[0] == "very_long"


def test_sound_in_a_segment_resets_the_carry(tmp_path):
    s = session(tmp_path)
    s._quiet_carry = 3000.0
    s._segment_t0 = time.time() - 1800
    s._carry_silence(FakeRecorder([track("system", 100.0)]), time.time())
    assert s._quiet_carry == 100.0

    s._quiet_carry = 3000.0
    s._segment_t0 = time.time() - 1800
    # 이번 세그먼트에 소리가 있었음(무음 100초 < 경과 1800초) — 앞 무음을 붙이지 않음
    assert s._silence_verdict(FakeRecorder([track("system", 100.0)])) is None


# --- 시간표 녹화 다시 시도 -----------------------------------------------------

class _Args:
    limit = 0
    language = None
    show = False
    no_upload = True
    dry_run = True
    label = "수동"
    course = ""
    once = False


def test_schedule_retries_the_same_class_after_a_backoff(tmp_path, monkeypatch):
    """예전: 다른 녹화가 잠금을 쥐어 시작 못 하면 '녹화되지 않았습니다' 로 끝 — 그 수업은 통째로 빔
    (그리고 끝나지 않은 같은 창을 쉬지 않고 다시 돌았음)."""
    from syncnote.recorder import schedule as sched

    class _Slot:
        name = "자료구조"
        language = None
        course_id = ""

    nxt = sched.Occurrence(_Slot(), datetime.now(), datetime.now() + timedelta(minutes=50))
    monkeypatch.setattr(sched, "parse", lambda _t: [_Slot()])
    monkeypatch.setattr(sched, "describe", lambda *_a, **_k: "")
    monkeypatch.setattr(sched, "next_window", lambda *a, **kw: nxt)
    waits = []

    def wait_until(when, **kw):
        waits.append(when)
        return len(waits) < 6          # 수업 시작 + 다시 시도 몇 번 뒤 끝냄

    monkeypatch.setattr(sched, "wait_until", wait_until)
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg({"recording.timetable": [{}]}),
                                                  Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    runs = []
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: runs.append(1))

    assert cli.cmd_record_schedule(_Args()) == 0
    assert len(runs) == 3, "같은 수업을 다시 시도하지 않았습니다"
    # waits: [시작, +5s, 시작, +15s, 시작, +30s] — 간격이 늘어남
    gaps = [(waits[i] - datetime.now()).total_seconds() for i in (1, 3, 5)]
    assert gaps[0] < gaps[1] < gaps[2] <= 31


# --- (C) 설정 오류 ---------------------------------------------------------------

def test_a_config_typo_does_not_stop_recording_commands(tmp_path):
    """예전: 설정 검사에 걸리면 record/stream 명령이 바로 끝남 — 그날 수업이 통째로 빔."""
    import pytest

    from syncnote import config as config_module

    p = tmp_path / "config.toml"
    p.write_text('[recording]\ncontainer = "mkvv"\n[recording.video]\nfps = 열\n',
                 encoding="utf-8")
    with pytest.raises(config_module.ConfigError):
        config_module.load(p)                       # 설정 명령은 그대로 엄격하게
    cfg = config_module.load(p, lenient=True)
    assert cfg.get("recording.container") == "mkv"
    assert any("기본값으로 대신" in n for n in cfg.load_notes)
    assert any("빼고 읽었습니다" in n for n in cfg.load_notes)


def test_recording_and_streaming_commands_read_config_leniently():
    parser = cli.build_parser()
    for argv in (["record", "start"], ["record", "schedule"], ["record", "watch"],
                 ["record", "guard"], ["record", "recover"], ["stream", "start"],
                 ["stream", "schedule"]):
        assert getattr(parser.parse_args(argv), "lenient_config", False), argv
    assert not getattr(parser.parse_args(["doctor"]), "lenient_config", False)
