"""부품이 아니라 **연결**을 검사합니다.

변이 테스트에서 드러난 것: 함수 하나하나는 잘 검사하는데, 그 부품을
잇는 자리(seam)가 끊겨도 전부 초록이었습니다. 각 PR 이 고치려던 바로
그 사고가 재발해도 테스트가 안 빨개집니다.

    S3  녹화 루프에서 _wait_for_video_start() 호출만 지움
    S5  시간표가 화면 시작을 pre-roll 만큼 당김
    S6  _run_recording 이 video_not_before 를 세션에 안 넘김
    U6  eclass sync 가 _upload_eclass 의 종료 코드를 버림

네 개 다 "부품은 멀쩡한데 안 불리거나 값이 안 전달됨" 입니다.
그래서 여기서는 `RecordingSession(video_not_before=X)` 가 아니라
`_run_recording(video_not_before=X)` → 세션에 뭐가 들어갔나 를 봅니다.

docs/TESTING.md 원칙 6.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import pytest

from syncnote import cli
from syncnote.paths import Layout


class Cfg:
    machine_label = "테스트PC"

    def __init__(self, d=None):
        self._d = d or {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def section(self, _k):
        return {}


# ---------------------------------------------------------------------
# S3 — 녹화 루프가 실제로 기다리는가
# ---------------------------------------------------------------------
class _FakeVideo:
    """ffmpeg 를 안 띄우고 '언제 start 가 불렸나' 만 기록합니다."""

    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.started_at_wall = None
        self._path = Path(kw["out_dir"]) / "v.mkv"
        _FakeVideo.instances.append(self)

    def start(self, stem):
        self.started_at_wall = time.time()
        self._path = Path(self.kw["out_dir"]) / f"{stem}.video.mkv"
        self._path.write_bytes(b"\x00" * 4096)
        return self._path

    def stop(self, timeout=20.0):
        return self._path

    @property
    def is_running(self):
        return True

    @property
    def started_at(self):
        return self.started_at_wall or 0.0


def _result():
    from syncnote.recorder.session import SessionResult

    return SessionResult("수업", datetime.now())


def _session(tmp_path, monkeypatch, *, video_not_before):
    from syncnote.recorder import session as S

    _FakeVideo.instances = []
    monkeypatch.setattr(S, "VideoRecorder", _FakeVideo)
    # 오디오와 합치기는 이 테스트의 관심사가 아닙니다.
    monkeypatch.setattr(S, "mux", lambda *a, **kw: None)
    from syncnote.recorder import ffmpeg_util
    monkeypatch.setattr(ffmpeg_util, "duration_of", lambda p: 1.0)
    return S.RecordingSession(
        Cfg({"recording.audio.enabled": False,
             "recording.video.enabled": True,
             "transcribe.enabled": False}),
        Layout(tmp_path), "수업", video_not_before=video_not_before,
    )


def test_the_recording_loop_actually_waits_before_starting_video(tmp_path, monkeypatch):
    """대기 함수만 따로 검사하면, 호출을 지워도 초록입니다(변이 S3).

    pre-roll 동안 강의실 PC 앞에 앉은 사람의 화면이 찍히는 사고가
    그대로 재발합니다. 여기서는 **녹화 루프를 실제로 돌려** 화면
    캡처가 그 시각 이후에 시작됐는지 봅니다.
    """
    deadline = time.time() + 0.6
    s = _session(tmp_path, monkeypatch, video_not_before=deadline)
    s._record_segment(1, tmp_path / "out", minutes=0.001, container="mkv",
                      video_on=True, audio_on=False,
                      result=_result())
    assert _FakeVideo.instances, "화면 녹화기가 만들어지지 않았습니다"
    began = _FakeVideo.instances[0].started_at_wall
    assert began is not None, "화면 녹화가 시작되지 않았습니다"
    assert began >= deadline - 0.05, (
        f"수업 시작 {deadline - began:.2f}초 전에 화면을 켰습니다 — "
        "그 사이 남의 화면이 찍힙니다"
    )


def test_without_a_deadline_the_loop_does_not_stall(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, video_not_before=0.0)
    t0 = time.time()
    s._record_segment(1, tmp_path / "out", minutes=0.001, container="mkv",
                      video_on=True, audio_on=False,
                      result=_result())
    assert time.time() - t0 < 1.0


# ---------------------------------------------------------------------
# S6 — _run_recording 이 세션까지 값을 넘기는가
# ---------------------------------------------------------------------
@pytest.fixture
def captured_session(monkeypatch, tmp_path):
    """RecordingSession 에 실제로 들어간 인자를 잡아 둡니다."""
    from syncnote.recorder import session as S

    seen = {}

    class _Spy(S.RecordingSession):
        def __init__(self, *a, **kw):
            seen.update(kw)
            super().__init__(*a, **kw)

        def run(self, *a, **kw):
            return S.SessionResult("수업", datetime.now())

    monkeypatch.setattr(S, "RecordingSession", _Spy)
    monkeypatch.setattr(cli, "_post_segment_factory", lambda *a, **kw: (lambda seg: None))
    return seen


def test_run_recording_forwards_video_not_before(captured_session, tmp_path):
    """끊기면 대기 로직이 멀쩡해도 기능 전체가 죽습니다(변이 S6).

    **_run_recording 을 부릅니다.** _run_recording_locked 를 직접
    부르면 그 둘 사이의 seam 을 건너뛰어서, 바깥쪽이 값을 버려도
    초록입니다 — 처음에 그렇게 썼다가 변이 S6 가 살아남았습니다.
    """
    cli._run_recording(
        Cfg({"transcribe.realtime.enabled": False}), Layout(tmp_path), "수업",
        minutes=0.001, upload=False, dry_run=True, video_not_before=12345.0,
    )
    assert captured_session.get("video_not_before") == 12345.0, captured_session


def test_run_recording_defaults_to_no_wait(captured_session, tmp_path):
    cli._run_recording(
        Cfg({"transcribe.realtime.enabled": False}), Layout(tmp_path), "수업",
        minutes=0.001, upload=False, dry_run=True,
    )
    assert captured_session.get("video_not_before", 0.0) == 0.0


# ---------------------------------------------------------------------
# S5 — 시간표가 '진짜 수업 시작' 을 계산하는가
# ---------------------------------------------------------------------
def test_schedule_delays_video_to_the_real_class_time(monkeypatch, tmp_path):
    """nxt.start 는 pre-roll 만큼 **당겨진** 시각입니다.

    그걸 그대로 넘기면 화면이 pre-roll 부터 찍힙니다 — #15 가 고친
    사고 그대로입니다(변이 S5).
    """
    from datetime import datetime, timedelta

    from syncnote.recorder import schedule as sched

    pre = 2.0
    real_start = datetime.now() + timedelta(minutes=5)
    shifted = real_start - timedelta(minutes=pre)

    class _Slot:
        name = "자료구조"
        language = None
        course_id = ""

    nxt = sched.Occurrence(_Slot(), shifted, real_start + timedelta(minutes=50))

    monkeypatch.setattr(sched, "parse", lambda _t: [_Slot()])
    monkeypatch.setattr(sched, "describe", lambda *_a, **_k: "")
    monkeypatch.setattr(sched, "next_window", lambda *a, **kw: nxt)
    monkeypatch.setattr(sched, "wait_until", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"recording.pre_roll_minutes": pre,
             "recording.post_roll_minutes": 3,
             "recording.timetable": [{}]}), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)

    seen = {}

    def _fake_run(cfg, layout, label, **kw):
        seen.update(kw)

    monkeypatch.setattr(cli, "_run_recording", _fake_run)

    class _Args:
        show = False
        once = True
        no_upload = True
        dry_run = True

    cli.cmd_record_schedule(_Args())

    got = seen.get("video_not_before")
    assert got, f"화면 시작 시각을 안 넘겼습니다: {seen}"
    assert abs(got - real_start.timestamp()) < 1.0, (
        f"화면을 수업 시작 {real_start.timestamp() - got:.0f}초 전에 켭니다 — "
        "pre-roll 만큼 당겨진 시각을 그대로 쓴 것입니다"
    )


# ---------------------------------------------------------------------
# U6 — eclass sync 가 업로드 종료 코드를 전파하는가
# ---------------------------------------------------------------------
# 지금까지 테스트는 _upload_eclass 를 **직접** 불렀습니다. 그래서
# cmd_eclass_sync 가 그 반환값을 버려도 전부 초록이었습니다.
# 11-12 사고가 정확히 그 모양이었습니다 — 업로드 0건, 종료 코드 0,
# CI 초록불.
class _NoopBrowser:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a, **kw):
        pass


class _NoopCrawler:
    def __init__(self, *a, **kw):
        pass

    def run(self, *a, **kw):
        from syncnote.eclass.crawler import Stats

        return Stats()


@pytest.fixture
def sync_args(monkeypatch, tmp_path):
    from syncnote.eclass import browser as br_mod
    from syncnote.eclass import crawler as cr_mod

    monkeypatch.setattr(br_mod, "EclassBrowser", _NoopBrowser)
    monkeypatch.setattr(cr_mod, "Crawler", _NoopCrawler)
    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"eclass.enabled": True,
             "eclass.upload.enabled": True,
             "github.enabled": True}), Layout(tmp_path)))

    class _Args:
        no_headless = False
        no_upload = False
        dry_run = False
        term = "2026-2"

    return _Args()


def test_sync_propagates_a_failing_upload_exit_code(sync_args, monkeypatch):
    """이게 끊기면 앞으로 모든 업로드 실패가 조용해집니다(변이 U6)."""
    monkeypatch.setattr(cli, "_upload_eclass", lambda *a, **kw: 1)
    assert cli.cmd_eclass_sync(sync_args) == 1


def test_sync_returns_zero_when_upload_is_fine(sync_args, monkeypatch):
    monkeypatch.setattr(cli, "_upload_eclass", lambda *a, **kw: 0)
    assert cli.cmd_eclass_sync(sync_args) == 0
