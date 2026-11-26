"""수업(시간표) 녹화가 감지 녹화에게서 녹화를 넘겨받음.

강의실 PC(HUM-2F-1244, 2026-11-26): 감지 대상에 chrome.exe·POWERPNT.EXE 등이 들어 있어 PC 를 켜고
Chrome 을 열자마자 '감지_chrome_0910' 이 시작해 하루 종일 녹화 잠금을 쥐었습니다. 그대로면 수업
시간이 되어도 시간표 녹화는 '다른 녹화가 돌고 있습니다' 로 시작하지 못하고, 감지 녹화는 수업 밖에서
시작해 과목이 없으니 과목 레포에도 올라가지 않습니다(사용자: '앱 실행 시 녹화 등이 수업 녹화를 먼저
가로채버린다').
"""

from __future__ import annotations

import json
import os
import threading
import time
import types

from syncnote import cli
from syncnote.paths import Layout
from syncnote.recorder import lock as L


def test_priorities():
    assert L.priority("schedule") > L.priority("watch")
    assert L.priority("manual") > L.priority("watch")
    assert L.priority("schedule") == L.priority("manual")      # 사람이 시작한 녹화는 빼앗지 않음
    # 예전 잠금 파일(trigger 없음)은 라벨로 짐작
    assert L.priority("", "감지_chrome_0910") == L.priority("watch")
    assert L.priority("", "자료구조") == L.priority("schedule")


def test_lock_file_records_the_trigger(tmp_path):
    p = tmp_path / "recording.lock"
    with L.hold(p, "감지_chrome_0910", "watch"):
        assert L.read(p).trigger == "watch"
        assert json.loads(p.read_text(encoding="utf-8"))["trigger"] == "watch"


def test_class_asks_a_watch_recording_to_hand_over(tmp_path):
    p = tmp_path / "recording.lock"
    with L.hold(p, "감지_chrome_0910", "watch"):
        holder = L.request_handover(p, "자료구조", "schedule")
        assert holder is not None and holder.label == "감지_chrome_0910"
        assert L.handover_requested(p, "watch", "감지_chrome_0910") == "자료구조"
        L.clear_handover(p)
        assert L.handover_requested(p, "watch") is None


def test_watch_never_takes_over_a_class_or_manual_recording(tmp_path):
    p = tmp_path / "recording.lock"
    for trigger in ("schedule", "manual"):
        with L.hold(p, "자료구조", trigger):
            assert L.request_handover(p, "감지_chrome_1000", "watch") is None
            assert L.handover_requested(p, trigger) is None


def test_an_old_request_is_ignored(tmp_path):
    p = tmp_path / "recording.lock"
    with L.hold(p, "감지_chrome_0910", "watch"):
        L.request_handover(p, "자료구조", "schedule")
        hp = p.with_name(p.name + ".handover")
        data = json.loads(hp.read_text(encoding="utf-8"))
        data["at"] = time.time() - 3600
        hp.write_text(json.dumps(data), encoding="utf-8")
        assert L.handover_requested(p, "watch") is None


def _cfg():
    return types.SimpleNamespace(get=lambda k, d=None: d, data_dir="")


def test_schedule_recording_takes_over_from_a_running_watch(tmp_path, monkeypatch):
    """감지 녹화가 잠금을 쥐고 있어도, 요청을 받고 비키면 수업 녹화가 시작됩니다."""
    layout = Layout(tmp_path)
    layout.ensure()
    lock_path = layout.state / "recording.lock"
    monkeypatch.setattr(cli, "HANDOVER_WAIT_SEC", 20)
    started = threading.Event()
    yielded = []

    def watch_side():
        with L.hold(lock_path, "감지_chrome_0910", "watch"):
            started.set()
            for _ in range(200):                     # 감지 녹화: 요청이 오면 비킴
                if L.handover_requested(lock_path, "watch", "감지_chrome_0910"):
                    yielded.append(time.time())
                    return
                time.sleep(0.05)

    t = threading.Thread(target=watch_side)
    t.start()
    started.wait(5)
    ran = []
    monkeypatch.setattr(cli, "_run_recording_locked",
                        lambda *a, **kw: ran.append(kw["trigger"]) or "recorded")
    out = cli._run_recording(_cfg(), layout, "자료구조", minutes=1, upload=False, dry_run=True,
                             trigger="schedule")
    t.join(5)
    assert yielded, "감지 녹화가 넘겨 달라는 요청을 받지 못했습니다"
    assert out == "recorded" and ran == ["schedule"]
    assert not (layout.state / "recording.lock.handover").exists()


def test_watch_does_not_start_while_a_class_records(tmp_path, monkeypatch):
    layout = Layout(tmp_path)
    layout.ensure()
    lock_path = layout.state / "recording.lock"
    monkeypatch.setattr(cli, "_run_recording_locked", lambda *a, **kw: "recorded")
    with L.hold(lock_path, "자료구조", "schedule"):
        out = cli._run_recording(_cfg(), layout, "감지_chrome_1000", minutes=1, upload=False,
                                 dry_run=True, trigger="watch")
    assert out is None
    assert not (layout.state / "recording.lock.handover").exists()


def test_a_running_watch_recording_stops_when_asked(tmp_path, monkeypatch):
    """감지 녹화 쪽: 녹화 중에 요청이 오면 지금 세그먼트를 마무리하고 끝냄(request_stop)."""
    layout = Layout(tmp_path)
    layout.ensure()
    lock_path = layout.state / "recording.lock"
    stopped = threading.Event()

    class FakeSession:
        def __init__(self, *a, **kw):
            pass

        def request_stop(self):
            stopped.set()

        def run(self, duration_minutes=None):
            stopped.wait(15)
            return types.SimpleNamespace(summary=lambda: "끝")

    monkeypatch.setattr("syncnote.recorder.session.RecordingSession", FakeSession)
    monkeypatch.setattr("syncnote.transcribe.realtime.maybe_start", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_post_segment_factory", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_recording_meta", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_store", lambda *a, **k: None)

    def ask_later():
        time.sleep(0.5)
        payload = {"label": "자료구조", "trigger": "schedule", "at": time.time(),
                   "for_pid": os.getpid()}
        lock_path.with_name("recording.lock.handover").write_text(json.dumps(payload),
                                                                 encoding="utf-8")

    threading.Thread(target=ask_later).start()
    t0 = time.time()
    with L.hold(lock_path, "감지_chrome_0910", "watch"):
        cli._run_recording_locked(_cfg(), layout, "감지_chrome_0910", minutes=None, upload=False,
                                  dry_run=True, trigger="watch", lock_path=lock_path)
    assert stopped.is_set() and time.time() - t0 < 10
