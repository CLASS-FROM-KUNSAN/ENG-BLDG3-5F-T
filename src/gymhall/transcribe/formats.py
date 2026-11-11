"""전사 결과를 txt / srt / vtt / json 으로 씁니다."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[dict[str, Any]] = field(default_factory=list)


def _timestamp(seconds: float, *, comma: bool = True) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def write_txt(path: Path, segments: Iterable[Segment], *, header: str = "") -> Path:
    lines = [header.rstrip(), ""] if header else []
    lines += [s.text.strip() for s in segments if s.text.strip()]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def write_srt(path: Path, segments: Iterable[Segment]) -> Path:
    out = []
    for i, seg in enumerate(segments, 1):
        if not seg.text.strip():
            continue
        out.append(str(i))
        out.append(f"{_timestamp(seg.start)} --> {_timestamp(seg.end)}")
        out.append(seg.text.strip())
        out.append("")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return Path(path)


def write_vtt(path: Path, segments: Iterable[Segment]) -> Path:
    out = ["WEBVTT", ""]
    for seg in segments:
        if not seg.text.strip():
            continue
        out.append(f"{_timestamp(seg.start, comma=False)} --> {_timestamp(seg.end, comma=False)}")
        out.append(seg.text.strip())
        out.append("")
    Path(path).write_text("\n".join(out), encoding="utf-8")
    return Path(path)


def write_json(path: Path, segments: Iterable[Segment], *, meta: dict | None = None) -> Path:
    payload = {"meta": meta or {}, "segments": [asdict(s) for s in segments]}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return Path(path)


WRITERS = {"txt": write_txt, "srt": write_srt, "vtt": write_vtt, "json": write_json}


def write_all(stem: Path, segments: list[Segment], formats: Iterable[str],
              *, header: str = "", meta: dict | None = None) -> list[Path]:
    written = []
    for fmt in formats:
        writer = WRITERS.get(fmt)
        if writer is None:
            continue
        target = Path(f"{stem}.{fmt}")
        if fmt == "txt":
            written.append(write_txt(target, segments, header=header))
        elif fmt == "json":
            written.append(write_json(target, segments, meta=meta))
        else:
            written.append(writer(target, segments))
    return written
