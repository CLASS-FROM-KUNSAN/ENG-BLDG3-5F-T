"""백업과 정리가 동시에 돌지 않게 하는 프로세스 간 잠금.

검토에서 재현된 일: 첫 백업이 몇 시간 걸리는 동안(03:13 시작) 05:43 의
정리가 SQLite 쓰기 잠금을 오래 쥐고 있었고, 백업은 매니페스트를 이미
GitHub 에 올린 뒤 'database is locked' 로 죽었습니다. 같은 seq 를 다시
쓰게 되고, 사슬이 갈라질 수 있습니다. 예약 작업 + 수동 실행처럼 백업
둘이 겹쳐도 같은 일이 납니다.

OS 잠금(msvcrt / fcntl)이라 프로세스가 죽으면 저절로 풀립니다.
잠금 파일이 남아 영원히 막히는 일이 없습니다.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _try_lock(fh) -> bool:
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive(path: Path, *, wait_sec: float = 0.0, poll_sec: float = 2.0) -> Iterator[bool]:
    """잠금을 얻으면 True, wait_sec 안에 못 얻으면 False 를 내줍니다.

    with exclusive(p, wait_sec=60) as got:
        if not got: ...
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as fh:
        deadline = time.monotonic() + max(0.0, wait_sec)
        got = _try_lock(fh)
        while not got and time.monotonic() < deadline:
            time.sleep(min(poll_sec, max(0.0, deadline - time.monotonic())))
            got = _try_lock(fh)
        try:
            yield got
        finally:
            if got:
                _unlock(fh)
