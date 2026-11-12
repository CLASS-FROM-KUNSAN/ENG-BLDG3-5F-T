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
    # PID 는 재사용됩니다. 'PID 가 살아 있다' 가 '그 녹화가 살아 있다' 는
    # 아닙니다. 프로세스 시작 시각을 같이 적어 두고 대조합니다.
    create_time: float = 0.0

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - self.started_at)


def _win_handle(pid: int):
    """PROCESS_QUERY_LIMITED_INFORMATION 핸들. 없으면 None.

    psutil 이 없는 윈도우에서도 '그 PID 가 살아 있는가' 를 답할 수
    있어야 합니다. 못 답하면 죽은 녹화가 남긴 잠금 때문에 하루 동안
    아무 녹화도 못 합니다(STALE_SEC).
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        return handle or None
    except Exception:
        return None


# OpenProcess 가 실패하는 이유는 둘입니다.
#   87  ERROR_INVALID_PARAMETER : 그런 PID 가 없음      -> 죽음
#    5  ERROR_ACCESS_DENIED     : 있는데 못 엶(다른 사용자) -> 살아 있음
# 구분하지 않고 '죽음' 으로 보면, 다른 계정이 돌리는 살아 있는 녹화의
# 잠금을 빼앗아 두 녹화가 같이 돕니다.
_ERROR_INVALID_PARAMETER = 87


def _win_alive(pid: int) -> bool | None:
    if os.name != "nt":
        return None
    handle = _win_handle(pid)
    if handle is None:
        try:
            import ctypes

            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        except Exception:
            return True          # 모르면 살아 있다고 봅니다(안전한 쪽)
    try:
        import ctypes

        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259          # STILL_ACTIVE
    except Exception:
        return None
    finally:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _win_create_time(pid: int) -> float:
    """GetProcessTimes 의 생성 시각(FILETIME, 100ns 단위)."""
    handle = _win_handle(pid)
    if handle is None:
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes

        created = wintypes.FILETIME()
        other = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(created),
            ctypes.byref(other[0]), ctypes.byref(other[1]), ctypes.byref(other[2]),
        )
        if not ok:
            return 0.0
        return float((created.dwHighDateTime << 32) | created.dwLowDateTime)
    except Exception:
        return 0.0
    finally:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _create_time(pid: int) -> float:
    """프로세스 시작 시각. 모르면 0.

    psutil 이 있으면 그걸 씁니다(윈도우 포함). 없으면 리눅스
    /proc/<pid>/stat 의 22번째 값(부팅 이후 틱)으로 대신합니다 — 절대
    시각이 아니어도 '같은 PID 의 다른 프로세스' 를 가르는 데는 충분합니다.
    둘 다 없으면 0, 즉 PID 재사용 검사를 포기합니다.
    """
    if pid <= 0:
        return 0.0
    try:
        import psutil  # type: ignore

        return float(psutil.Process(pid).create_time())
    except Exception:
        pass
    if os.name == "nt":
        return _win_create_time(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        # comm 에 공백이 들어갈 수 있어 마지막 ')' 뒤부터 셉니다.
        fields = stat[stat.rindex(")") + 2:].split()
        return float(fields[19])            # starttime = 22번째 필드
    except Exception:
        return 0.0


def _pid_exists(pid: int) -> bool:
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        got = _win_alive(pid)
        # 커널에 물어봐서 모르겠다고 하면 그때만 '살아 있다' 로 봅니다.
        return True if got is None else got
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive(pid: int, create_time: float = 0.0) -> bool:
    """그 PID 가, 그리고 **그때 그 프로세스가** 살아 있는지.

    시작 시각 비교는 psutil 이 있든 없든 똑같이 합니다. 예전에는 이
    비교가 psutil 가지 안에만 있어서, psutil 이 없는 환경에서는 PID
    재사용 검사가 통째로 꺼졌습니다.
    """
    if pid <= 0:
        return False
    if not _pid_exists(pid):
        return False
    if create_time:
        now = _create_time(pid)
        # psutil(초)과 윈도우 FILETIME(100ns)은 단위가 다릅니다. 저장할
        # 때와 비교할 때 같은 _create_time 을 쓰므로 자릿수는 같습니다.
        # 다만 허용 오차는 상대값으로 둡니다.
        tol = max(1.0, abs(create_time) * 1e-6)
        if now and abs(now - create_time) > tol:
            return False         # PID 만 재사용된 남의 프로세스
    return True


def read(path: Path) -> LockInfo | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return LockInfo(int(data.get("pid", 0)), str(data.get("label", "")),
                        float(data.get("started_at", 0.0)),
                        float(data.get("create_time", 0.0)))
    except (TypeError, ValueError):
        return None


def _stale(info: LockInfo | None) -> bool:
    if info is None:
        return True
    # 자기 PID 라고 무조건 빼앗으면 안 됩니다. 같은 프로세스가 녹화를
    # 두 번 시작하는 것도 막아야 할 일입니다. 예전 실행이 같은 PID 로
    # 남긴 잠금은 create_time 이 달라서 아래에서 걸러집니다.
    if info.age_sec > STALE_SEC:
        return True
    return not _alive(info.pid, info.create_time)


def _try_create(path: Path, label: str) -> bool:
    """**원자적으로** 만듭니다. 이미 있으면 False.

    exists() 로 보고 write_text() 하면 두 프로세스가 둘 다 '없다' 를
    보고 둘 다 씁니다. O_CREAT|O_EXCL 는 커널이 하나만 통과시킵니다.
    """
    payload = {
        "pid": os.getpid(),
        "label": label,
        "started_at": time.time(),
        "create_time": _create_time(os.getpid()),
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False))
    return True


@contextmanager
def hold(path: Path, label: str):
    """녹화 잠금을 잡습니다. 이미 살아 있는 녹화가 있으면 RecordingBusy."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 한 번은 그냥, 한 번은 죽은 잠금을 치운 뒤. 두 번이면 충분합니다 —
    # 치운 직후 남이 먼저 잡았다면 그쪽이 진짜 녹화 중입니다.
    for attempt in (1, 2):
        if _try_create(path, label):
            break
        current = read(path)
        if current is not None and not _stale(current):
            raise RecordingBusy(
                f"이미 녹화 중입니다(PID {current.pid}, '{current.label}', "
                f"{current.age_sec / 60:.0f}분째). 두 녹화가 같은 오디오 장치를 "
                "열면 양쪽 다 무음이 됩니다."
            )
        if attempt == 2:
            raise RecordingBusy("녹화 잠금을 잡지 못했습니다(경쟁).")
        if current is not None:
            log.info("남아 있던 잠금을 치웁니다(PID %d, '%s').",
                     current.pid, current.label)
        path.unlink(missing_ok=True)
    try:
        yield
    finally:
        # 내 잠금일 때만 지웁니다. 빼앗긴 뒤에 지우면 남의 것을 지웁니다.
        latest = read(path)
        if latest is not None and latest.pid == os.getpid():
            path.unlink(missing_ok=True)
