"""올릴 녹화 파일 목록(보낼 편지함).

세그먼트 파일이 만들어지면 먼저 여기에 적고, 릴리스에 올라가면 지웁니다. 올리는 도중
컴퓨터가 꺼지거나 네트워크가 끊겨도 목록에 남아 있으므로 다음에 시작할 때
(`record schedule`/`record watch`/`record recover`) 다시 올립니다.

    <data_dir>/state/recording_outbox.json
    [{"path": "...mkv", "course_id": "", "label": "자료구조", "day": "2026-11-23", "added": ...}]
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..logging_setup import get as get_logger
from ..state import _atomic_write

log = get_logger("recorder.outbox")

FILE = "recording_outbox.json"
_LOCK = threading.Lock()          # 같은 프로세스 안(녹화 콜백 ↔ 복구 스레드)


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE


def items(state_dir: Path) -> list[dict]:
    p = _path(state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("올릴 목록 %s 을 읽지 못해 빈 목록으로 봅니다: %s", p.name, exc)
        return []
    return [d for d in data if isinstance(d, dict) and d.get("path")] if isinstance(data, list) else []


def _save(state_dir: Path, data: list[dict]) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    _atomic_write(_path(state_dir), json.dumps(data, ensure_ascii=False, indent=1))


def add(state_dir: Path, path: Path, *, course_id: str = "", label: str = "", day: str = "") -> None:
    with _LOCK:
        data = items(state_dir)
        key = str(Path(path))
        if any(d["path"] == key for d in data):
            return
        data.append({"path": key, "course_id": course_id, "label": label, "day": day,
                     "added": time.time()})
        _save(state_dir, data)


def remove(state_dir: Path, path: Path) -> None:
    with _LOCK:
        data = items(state_dir)
        key = str(Path(path))
        left = [d for d in data if d["path"] != key]
        if len(left) != len(data):
            _save(state_dir, left)
