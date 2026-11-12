"""전사 대기열.

faster-whisper 없이 대기열 로직만 검증합니다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from gymhall.transcribe.batch import QUEUE_FILE, Queue


def make_queue(tmp_path) -> Queue:
    return Queue(tmp_path / QUEUE_FILE)


def test_add_and_persist(tmp_path):
    q = make_queue(tmp_path)
    q.add(tmp_path / "a.wav", label="자료구조", out_dir=tmp_path / "out", track="system")
    assert len(q) == 1

    again = make_queue(tmp_path)
    assert len(again) == 1
    assert again.items[0]["label"] == "자료구조"
    assert again.items[0]["track"] == "system"


def test_duplicate_add_is_ignored(tmp_path):
    """같은 녹화를 두 번 등록하면 전사를 두 번 돌리게 됩니다."""
    q = make_queue(tmp_path)
    for _ in range(3):
        q.add(tmp_path / "a.wav", label="x", out_dir=tmp_path)
    assert len(q) == 1


def test_remove(tmp_path):
    q = make_queue(tmp_path)
    q.add(tmp_path / "a.wav", label="x", out_dir=tmp_path)
    q.add(tmp_path / "b.wav", label="y", out_dir=tmp_path)
    q.remove(str(tmp_path / "a.wav"))
    assert len(q) == 1
    assert make_queue(tmp_path).items[0]["audio"].endswith("b.wav")


def test_pop_all_clears(tmp_path):
    q = make_queue(tmp_path)
    q.add(tmp_path / "a.wav", label="x", out_dir=tmp_path)
    items = q.pop_all()
    assert len(items) == 1
    assert len(q) == 0
    assert len(make_queue(tmp_path)) == 0


def test_corrupt_queue_is_recreated(tmp_path):
    """대기열 파일이 깨졌다고 녹화가 막히면 안 됩니다."""
    path = tmp_path / QUEUE_FILE
    path.write_text("이건 JSON 이 아닙니다", encoding="utf-8")
    q = Queue(path)
    assert len(q) == 0
    q.add(tmp_path / "a.wav", label="x", out_dir=tmp_path)
    assert len(Queue(path)) == 1


def test_describe_empty(tmp_path):
    assert "비어 있습니다" in make_queue(tmp_path).describe()


def test_describe_marks_missing_file(tmp_path):
    q = make_queue(tmp_path)
    q.add(tmp_path / "ghost.wav", label="x", out_dir=tmp_path)
    assert "파일 없음" in q.describe()


def test_describe_shows_size(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"\0" * (3 * 1024 * 1024))
    q = make_queue(tmp_path)
    q.add(f, label="x", out_dir=tmp_path)
    assert "3 MB" in q.describe()


# ---------------------------------------------------------------------------
class FakeSegment:
    def __init__(self, wavs, started_at=None):
        self.wav_paths = wavs
        self.started_at = started_at or datetime(2026, 11, 11, 9, 0)
        self.index = 1
        self.path = Path("x.mkv")
        self.seconds = 60.0


class FakeConfig:
    def __init__(self, **over):
        self.values = {
            "transcribe.enabled": True,
            "transcribe.batch.enabled": True,
            "transcribe.source_track": "system",
            "transcribe.batch.mode": "queue",
        }
        self.values.update(over)

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_enqueue_picks_requested_track(tmp_path):
    from gymhall.paths import Layout
    from gymhall.transcribe.batch import enqueue_segment

    layout = Layout(tmp_path)
    layout.ensure()
    seg = FakeSegment({"system": tmp_path / "s.wav", "mic": tmp_path / "m.wav"})

    enqueue_segment(FakeConfig(), layout, seg, "자료구조")
    q = Queue(layout.state / QUEUE_FILE)
    assert len(q) == 1
    assert q.items[0]["track"] == "system"


def test_enqueue_both_tracks(tmp_path):
    from gymhall.paths import Layout
    from gymhall.transcribe.batch import enqueue_segment

    layout = Layout(tmp_path)
    layout.ensure()
    seg = FakeSegment({"system": tmp_path / "s.wav", "mic": tmp_path / "m.wav"})

    enqueue_segment(FakeConfig(**{"transcribe.source_track": "both"}), layout, seg, "x")
    assert len(Queue(layout.state / QUEUE_FILE)) == 2


def test_enqueue_noop_when_disabled(tmp_path):
    from gymhall.paths import Layout
    from gymhall.transcribe.batch import enqueue_segment

    layout = Layout(tmp_path)
    layout.ensure()
    seg = FakeSegment({"system": tmp_path / "s.wav"})
    enqueue_segment(FakeConfig(**{"transcribe.enabled": False}), layout, seg, "x")
    assert len(Queue(layout.state / QUEUE_FILE)) == 0


def test_enqueue_noop_when_track_absent(tmp_path):
    """마이크만 녹음했는데 system 을 전사하라고 하면 조용히 넘어갑니다."""
    from gymhall.paths import Layout
    from gymhall.transcribe.batch import enqueue_segment

    layout = Layout(tmp_path)
    layout.ensure()
    seg = FakeSegment({"mic": tmp_path / "m.wav"})
    enqueue_segment(FakeConfig(), layout, seg, "x")
    assert len(Queue(layout.state / QUEUE_FILE)) == 0


# ---------------------------------------------------------------------------
# 환각 필터가 실제로 전사 경로에 연결돼 있는지
# ---------------------------------------------------------------------------
class _Seg:
    def __init__(self, start, end, text, no_speech_prob=0.1):
        self.start = start
        self.end = end
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.words = None


class _Info:
    language = "ko"
    duration = 60.0


class _Model:
    def __init__(self, segments):
        self.segments = segments

    def transcribe(self, path, **kw):
        return iter(self.segments), _Info()


def test_transcribe_file_drops_hallucinations(tmp_path):
    """파일로 떨어지는 결과에 상투어가 남으면 안 됩니다."""
    from gymhall.transcribe.batch import transcribe_file
    from gymhall.transcribe.cleanup import Cleaner
    from gymhall.transcribe.engine import EngineSpec

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\0" * 100)

    model = _Model([
        _Seg(0, 3, "자 오늘은 자료구조입니다"),
        _Seg(3, 6, "시청해주셔서 감사합니다"),        # 상투어
        _Seg(6, 9, "어", 0.97),                        # 무음
        _Seg(9, 12, "트리의 높이는 log n 입니다"),
    ])

    result = transcribe_file(
        audio, tmp_path / "out", spec=EngineSpec(), model=model,
        output_formats=("txt", "json"), cleaner=Cleaner(),
    )
    text = (tmp_path / "out.txt").read_text(encoding="utf-8")
    assert "자료구조" in text
    assert "log n" in text
    assert "시청해주셔서" not in text
    assert len(result.outputs) == 2


def test_cleanup_counts_land_in_json_meta(tmp_path):
    import json

    from gymhall.transcribe.batch import transcribe_file
    from gymhall.transcribe.cleanup import Cleaner
    from gymhall.transcribe.engine import EngineSpec

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\0" * 100)
    model = _Model([
        _Seg(0, 3, "정상 문장"),
        _Seg(3, 6, "구독과 좋아요"),
    ])
    transcribe_file(audio, tmp_path / "out", spec=EngineSpec(), model=model,
                    output_formats=("json",), cleaner=Cleaner())
    meta = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["meta"]
    assert meta["cleanup"]["kept"] == 1
    assert meta["cleanup"]["dropped_phrase"] == 1


def test_disabled_cleaner_keeps_hallucinations(tmp_path):
    """끄면 그대로 남아야 합니다 — 사용자가 끌 수 있다는 것을 보장."""
    from gymhall.transcribe.batch import transcribe_file
    from gymhall.transcribe.cleanup import Cleaner
    from gymhall.transcribe.engine import EngineSpec

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"\0" * 100)
    model = _Model([_Seg(0, 3, "시청해주셔서 감사합니다")])
    transcribe_file(audio, tmp_path / "out", spec=EngineSpec(), model=model,
                    output_formats=("txt",), cleaner=Cleaner(enabled=False))
    assert "시청해주셔서" in (tmp_path / "out.txt").read_text(encoding="utf-8")
