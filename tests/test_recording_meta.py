"""녹화 파일 안 메타데이터(태그·챕터) — 겹치든 안 겹치든 항상 남김."""

import json
import struct
import subprocess
import time
import wave
from datetime import datetime, timedelta

import pytest

from syncnote import cli
from syncnote.paths import Layout
from syncnote.recorder import schedule as S
from syncnote.recorder.meta import ClassMeta, RecordingMeta, ffmetadata

T0 = datetime(2026, 11, 23, 8, 58)


def _merged():
    return RecordingMeta(
        "자료구조 + 영어2", "schedule",
        [ClassMeta("자료구조", datetime(2026, 11, 23, 9, 0), datetime(2026, 11, 23, 10, 30),
                   course_id="C1", room="공학관 401호", professor="김교수"),
         ClassMeta("영어2", datetime(2026, 11, 23, 10, 0), datetime(2026, 11, 23, 11, 0),
                   language="en")],
        policy="merge", window_start=T0, window_end=datetime(2026, 11, 23, 11, 3),
        pre_roll_min=2, post_roll_min=3, machine="강의실PC", version="0.1.0")


def _tl(meta):
    return [(n, f"{a:%H:%M}", f"{b:%H:%M}") for n, a, b in meta.timeline()]


def test_timeline_marks_each_class_and_the_overlap():
    assert _tl(_merged()) == [
        ("수업 전 여유", "08:58", "09:00"), ("자료구조", "09:00", "10:00"),
        ("자료구조 + 영어2 (겹침)", "10:00", "10:30"), ("영어2", "10:30", "11:00"),
        ("수업 후 여유", "11:00", "11:03")]


def test_single_class_still_gets_full_metadata():
    m = RecordingMeta("운동과건강", "schedule",
                      [ClassMeta("운동과건강", datetime(2026, 11, 27, 13, 0),
                                 datetime(2026, 11, 27, 14, 15), room="체육관")],
                      window_start=datetime(2026, 11, 27, 12, 58),
                      window_end=datetime(2026, 11, 27, 14, 18), pre_roll_min=2, post_roll_min=3)
    assert [n for n, *_ in m.timeline()] == ["수업 전 여유", "운동과건강", "수업 후 여유"]
    tags = m.tags(datetime(2026, 11, 27, 12, 58), datetime(2026, 11, 27, 13, 28), 1)
    assert "SYNCNOTE_OVERLAP_POLICY" not in tags                     # 안 겹치면 정책 없음
    cls = json.loads(tags["SYNCNOTE_CLASSES"])
    assert cls == [{"name": "운동과건강", "start": cls[0]["start"], "end": cls[0]["end"],
                    "room": "체육관", "day": "금"}]
    assert cls[0]["start"].startswith("2026-11-27T13:00:00") and "+" in cls[0]["start"]
    assert "체육관" in tags["COMMENT"] and "시간표 자동 녹화" in tags["COMMENT"]


def test_tags_carry_both_classes_policy_and_segment_times():
    tags = _merged().tags(datetime(2026, 11, 23, 9, 28), datetime(2026, 11, 23, 9, 58), 2)
    assert tags["TITLE"] == "자료구조 + 영어2 2026-11-23 08:58~11:03"
    assert tags["SYNCNOTE_SEGMENT"] == "2" and tags["SYNCNOTE_OVERLAP_POLICY"] == "merge"
    assert tags["SYNCNOTE_TRIGGER"] == "schedule" and tags["SYNCNOTE_MACHINE"] == "강의실PC"
    assert tags["SYNCNOTE_SEGMENT_START"].startswith("2026-11-23T09:28:00")
    names = [c["name"] for c in json.loads(tags["SYNCNOTE_CLASSES"])]
    assert names == ["자료구조", "영어2"]
    assert "하나로 이어서" in tags["COMMENT"] and "과목코드 C1" in tags["COMMENT"]
    assert all(v != "" for v in tags.values())


def test_chapters_are_clipped_to_each_segment_file():
    m = _merged()
    seg_start = datetime(2026, 11, 23, 9, 58).timestamp()            # 두 번째 30분 파일
    seg_end = datetime(2026, 11, 23, 10, 28).timestamp()
    ch = m.chapters(seg_start, seg_end)
    assert ch == [("자료구조", 0, 120_000), ("자료구조 + 영어2 (겹침)", 120_000, 1_800_000)]


def test_ffmetadata_escapes_special_characters():
    text = ffmetadata({"COMMENT": "a=b;c#d\\e\n둘째 줄"}, [("수업 = 1", 0, 1000)])
    assert "COMMENT=a\\=b\\;c\\#d\\\\e\\\n둘째 줄" in text
    assert "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=수업 \\= 1" in text


# -- CLI 가 메타데이터를 만들어 넘기는지 ------------------------------------
class Cfg:
    machine_label = "강의실PC"

    def __init__(self, d):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def section(self, _k):
        return {}


ROWS = [{"name": "A", "day": "mon", "start": "09:00", "end": "10:30", "room": "공학관 401호",
         "professor": "김교수", "course_id": "C1"},
        {"name": "B", "day": "mon", "start": "10:00", "end": "11:00", "language": "en"}]


def test_schedule_meta_from_the_planned_window():
    cfg = Cfg({"recording.timetable": ROWS, "recording.pre_roll_minutes": 2,
               "recording.post_roll_minutes": 3})
    occ = S.next_window(S.parse(ROWS), now=datetime(2026, 11, 23, 8, 0), pre_roll_min=2,
                        post_roll_min=3)
    course = type("C", (), {"id": "C1", "name": "자료구조", "slug": "me/ds-2026"})()
    m = cli._recording_meta(cfg, occ.name, "schedule", occ=occ, course=course)
    assert m.label == "A + B" and m.policy == "merge" and m.trigger == "schedule"
    assert [(c.name, c.room, c.professor, c.course_id, c.language) for c in m.classes] == [
        ("A", "공학관 401호", "김교수", "C1", ""), ("B", "", "", "", "en")]   # B 에 A 과목을 안 붙임
    assert m.classes[0].repo == "me/ds-2026" and m.classes[1].repo == ""
    assert (m.window_start, m.window_end) == (occ.start, occ.end) and m.pre_roll_min == 2


def test_manual_meta_picks_the_class_on_now_or_none():
    cfg = Cfg({"recording.timetable": ROWS})
    during = cli._recording_meta(cfg, "수동_0915", "manual", when=datetime(2026, 11, 23, 9, 15))
    assert [c.name for c in during.classes] == ["A"] and during.policy == ""
    both = cli._recording_meta(cfg, "수동_1015", "manual", when=datetime(2026, 11, 23, 10, 15))
    assert [c.name for c in both.classes] == ["A", "B"] and both.policy == "merge"
    off = cli._recording_meta(cfg, "수동_2000", "manual", when=datetime(2026, 11, 23, 20, 0))
    assert off.classes == [] and "시간표 밖" in off.summary()
    assert off.pre_roll_min == 0                                      # 수동은 여유 시간 없음


@pytest.fixture
def captured_session(monkeypatch):
    from syncnote.recorder import session as SS

    seen = {}

    class _Spy(SS.RecordingSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["meta"] = self.meta

        def run(self, *a, **kw):
            return SS.SessionResult("수업", datetime.now())

    monkeypatch.setattr(SS, "RecordingSession", _Spy)
    monkeypatch.setattr(cli, "_post_segment_factory", lambda *a, **kw: (lambda seg: None))
    return seen


def test_every_recording_gets_metadata_even_without_schedule(captured_session, tmp_path):
    cli._run_recording(Cfg({"transcribe.realtime.enabled": False}), Layout(tmp_path), "수동",
                       minutes=0.001, upload=False, dry_run=True)
    m = captured_session["meta"]
    assert isinstance(m, RecordingMeta) and m.trigger == "manual" and m.label == "수동"


def test_explicit_meta_reaches_the_session(captured_session, tmp_path):
    meta = _merged()
    cli._run_recording(Cfg({"transcribe.realtime.enabled": False}), Layout(tmp_path), "A + B",
                       minutes=0.001, upload=False, dry_run=True, meta=meta)
    assert captured_session["meta"] is meta


# -- 실제 ffmpeg 로 파일 안에 들어가는지 --------------------------------------
def _ffmpeg_or_skip():
    from syncnote.recorder.ffmpeg_util import find_ffmpeg, find_ffprobe

    try:
        find_ffmpeg()
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg 없음")
    probe = find_ffprobe()
    if not probe:
        pytest.skip("ffprobe 없음")
    return probe


@pytest.mark.parametrize("ext", ["mkv", "mp4"])
def test_tags_and_chapters_are_inside_the_file(tmp_path, ext):
    from syncnote.recorder.mux import MuxInput, mux

    probe = _ffmpeg_or_skip()
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<h", 0) * 8000 * 20)
    base = time.time()
    t0 = datetime.fromtimestamp(base)
    meta = RecordingMeta("자료구조 + 영어2", "schedule", [
        ClassMeta("자료구조", t0 + timedelta(seconds=2), t0 + timedelta(seconds=12), room="401호"),
        ClassMeta("영어2", t0 + timedelta(seconds=8), t0 + timedelta(seconds=18))],
        policy="merge", window_start=t0, window_end=t0 + timedelta(seconds=20),
        machine="PC=1;#x")
    out = tmp_path / f"out.{ext}"
    mux(out, None, 0.0, [MuxInput(wav, "mic", base)], delete_audio=False, meta=meta,
        segment_index=1, ended_at=base + 20)
    assert not list(tmp_path.glob("*.ffmeta.txt"))                    # 임시 파일 정리
    r = subprocess.run([probe, "-v", "error", "-show_chapters", "-show_format", "-of", "json",
                        str(out)], capture_output=True, text=True, encoding="utf-8")
    j = json.loads(r.stdout)
    chapters = [(c["tags"]["title"], round(float(c["start_time"])), round(float(c["end_time"])))
                for c in j["chapters"]]
    assert chapters == [("수업 전 여유", 0, 2), ("자료구조", 2, 8),
                        ("자료구조 + 영어2 (겹침)", 8, 12), ("영어2", 12, 18), ("수업 후 여유", 18, 20)]
    tags = {k.upper(): v for k, v in j["format"]["tags"].items()}
    assert tags["SYNCNOTE_MACHINE"] == "PC=1;#x" and tags["SYNCNOTE_OVERLAP_POLICY"] == "merge"
    assert [c["name"] for c in json.loads(tags["SYNCNOTE_CLASSES"])] == ["자료구조", "영어2"]
    assert "401호" in tags["COMMENT"]
