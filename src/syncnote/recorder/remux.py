"""ffmpeg 없이 소리만 녹음한 세그먼트를, ffmpeg 가 생긴 뒤 파일(mkv/mp4)로 만듭니다.

녹음할 때(session.py) ffmpeg 가 없으면 WAV 와 함께 `<세그먼트>.pending.json` 을
남깁니다. 거기에 만들 파일 경로, 트랙별 WAV·시작 시각, 트랙 배치, 수업 메타데이터
(태그·챕터용), 세그먼트 번호·끝 시각, 다 만든 뒤 WAV 를 지울지, 과목이 적혀 있습니다.

    python -m syncnote record remux [--upload] [--dry-run]

화면은 처음부터 잡히지 않았으므로 되살릴 수 없습니다. 만들어지는 파일은 소리
트랙만 든 mkv/mp4 이고, 수업 태그·챕터는 평소 녹화와 똑같이 들어갑니다.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.remux")

SUFFIX = ".pending.json"


def pending_path(final: Path) -> Path:
    return Path(final).with_name(Path(final).stem + SUFFIX)


def write_pending(final: Path, tracks: list[tuple[str, Path, float]], *, layout: str,
                  meta=None, segment_index: int = 1, ended_at: float | None = None,
                  delete_wav: bool = False, course_id: str = "",
                  started: datetime | None = None) -> Path:
    data = {
        "version": 1,
        "final": str(final),
        "layout": layout,
        "tracks": [{"name": n, "path": str(p), "started_at": s} for n, p, s in tracks],
        "meta": meta.to_state() if meta is not None else None,
        "segment_index": segment_index,
        "ended_at": ended_at,
        "delete_wav": delete_wav,
        "course_id": course_id,
        "started": started.isoformat() if started else "",
        "reason": "ffmpeg 없음",
    }
    p = pending_path(final)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log.warning("파일은 ffmpeg 가 생긴 뒤 만듭니다: %s", p.name)
    return p


def find_pending(root: Path) -> list[Path]:
    root = Path(root)
    return sorted(root.rglob("*" + SUFFIX)) if root.is_dir() else []


@dataclass
class Remuxed:
    final: Path
    course_id: str = ""
    seconds: float = 0.0
    started: datetime | None = None
    tracks: list[str] = field(default_factory=list)


def remux_one(pending: Path, *, mux: Callable | None = None) -> Remuxed:
    """pending 하나 -> 파일. 성공하면 pending 을 지웁니다. 실패하면 예외(pending 은 그대로)."""
    from .meta import RecordingMeta
    from .mux import MuxInput
    from .mux import mux as real_mux

    mux = mux or real_mux
    d = json.loads(Path(pending).read_text(encoding="utf-8"))
    inputs = [MuxInput(Path(t["path"]), t["name"], float(t.get("started_at") or 0))
              for t in d.get("tracks", [])]
    missing = [i.path.name for i in inputs if not i.path.exists()]
    if missing:
        raise FileNotFoundError(f"WAV 가 없습니다: {', '.join(missing)}")
    meta = RecordingMeta.from_state(d["meta"]) if d.get("meta") else None
    final = Path(d["final"])
    mux(final, None, 0.0, inputs, layout=d.get("layout", "separate"), delete_video=False,
        delete_audio=bool(d.get("delete_wav")), meta=meta,
        segment_index=int(d.get("segment_index", 1)), ended_at=d.get("ended_at"))
    Path(pending).unlink(missing_ok=True)
    starts = [i.started_at for i in inputs if i.started_at]
    seconds = max(0.0, float(d.get("ended_at") or 0) - min(starts)) if starts else 0.0
    started = datetime.fromisoformat(d["started"]) if d.get("started") else None
    log.info("만들었습니다: %s", final)
    return Remuxed(final, str(d.get("course_id", "")), seconds, started,
                   [i.name for i in inputs])


def remux_all(root: Path, *, mux: Callable | None = None,
              say: Callable[[str], None] = print) -> tuple[list[Remuxed], list[tuple[Path, str]]]:
    done, failed = [], []
    for p in find_pending(root):
        try:
            done.append(remux_one(p, mux=mux))
            say(f"  [O] {done[-1].final.name}")
        except Exception as exc:  # noqa: BLE001 — 하나가 실패해도 나머지는 계속
            failed.append((p, str(exc)))
            say(f"  [X] {p.name}: {exc}")
    return done, failed
