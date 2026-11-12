"""한 PC 에서 녹화는 한 번에 하나만.

RecordSchedule 과 RecordWatch 는 서로 다른 작업이라 작업 스케줄러의
-MultipleInstances IgnoreNew 가 둘 사이를 막아 주지 않습니다. Zoom 이
켜진 채로 수업 시간이 되면 두 녹화가 같이 시작되고, ffmpeg 두 개가 같은
마이크·시스템 오디오를 열려고 합니다. 한쪽이 장치를 못 잡고 죽거나
**양쪽 다 무음**으로 끝납니다. 둘 다 '녹화 중' 으로 보입니다.

잠금 파일에 PID 와 시작 시각을 적어 둡니다. 프로세스가 죽어 파일만
남은 경우(크래시, 강제 종료)는 PID 가 살아 있는지 보고 빼앗습니다.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.lock")

# 이보다 오래된 잠금은 PID 확인에 실패해도 빼앗습니다. PID 는 재사용되므로
# '살아 있다' 가 항상 '그 녹화가 살아 있다' 를 뜻하지는 않습니다.
STALE_SEC = 24 * 3600


class RecordingBusy(RuntimeError):
    """다른 녹화가 이미 돌고 있습니다."""


@dataclass(frozen=True)
class LockInfo:
    pid: int
    label: str
    started_at: float

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.started_at)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return psutil.pid_exists(pid)
    except Exception:
        pass
    if os.name == "nt":                      # psutil 이 없을 때의 대비책
        return True                          # 모르면 '살아 있다' 로 봅니다
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(path: Path) -> LockInfo | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return LockInfo(int(data.get("pid", 0)), str(data.get("label", "")),
                        float(data.get("started_at", 0.0)))
    except (TypeError, ValueError):
        return None


def _stale(info: LockInfo | None) -> bool:
    if info is None:
        return True
    if info.pid == os.getpid():
        return True                          # 내가 남긴 것
    if info.age_sec > STALE_SEC:
        return True
    return not _alive(info.pid)


@contextmanager
def hold(path: Path, label: str):
    """녹화 잠금을 잡습니다. 이미 살아 있는 녹화가 있으면 RecordingBusy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read(path)
    if not _stale(current):
        raise RecordingBusy(
            f"이미 녹화 중입니다(PID {current.pid}, '{current.label}', "
            f"{current.age_sec / 60:.0f}분째). 두 녹화가 같은 오디오 장치를 "
            "열면 양쪽 다 무음이 됩니다."
        )
    if current is not None:
        log.info("남아 있던 잠금을 빼앗습니다(PID %d, '%s').", current.pid, current.label)

    payload = {"pid": os.getpid(), "label": label, "started_at": time.time()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        yield
    finally:
        # 내 잠금일 때만 지웁니다. 빼앗긴 뒤에 지우면 남의 것을 지웁니다.
        latest = read(path)
        if latest is not None and latest.pid == os.getpid():
            path.unlink(missing_ok=True)
