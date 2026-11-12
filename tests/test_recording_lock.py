"""한 PC 에서 녹화는 한 번에 하나만.

RecordSchedule 과 RecordWatch 는 서로 다른 작업이라 작업 스케줄러의
-MultipleInstances 가 둘 사이를 막지 않습니다. 둘이 같이 돌면 ffmpeg
두 개가 같은 오디오 장치를 열어 **양쪽 다 무음**으로 끝납니다.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import time

import pytest

from gymhall.recorder import lock as L


def test_a_second_recording_is_refused(tmp_path):
    path = tmp_path / "recording.lock"
    with L.hold(path, "수업_0900"):
        with pytest.raises(L.RecordingBusy, match="이미 녹화 중"):
            with L.hold(path, "감지_Zoom"):
                pass


def test_the_lock_is_released_afterwards(tmp_path):
    path = tmp_path / "recording.lock"
    with L.hold(path, "A"):
        pass
    assert not path.exists()
    with L.hold(path, "B"):
        pass


def test_the_lock_is_released_even_after_an_error(tmp_path):
    path = tmp_path / "recording.lock"
    with pytest.raises(RuntimeError), L.hold(path, "A"):
        raise RuntimeError("녹화 실패")
    assert not path.exists()
    with L.hold(path, "B"):
        pass


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    """크래시로 남은 잠금 때문에 영영 녹화를 못 하면 안 됩니다."""
    path = tmp_path / "recording.lock"
    path.write_text(json.dumps({
        "pid": 999_999_999, "label": "죽은세션", "started_at": time.time(),
    }), encoding="utf-8")
    with L.hold(path, "새세션"):
        assert L.read(path).pid == os.getpid()


def test_a_reused_pid_does_not_look_alive(tmp_path):
    """PID 는 재사용됩니다. 시작 시각이 다르면 남의 프로세스입니다."""
    path = tmp_path / "recording.lock"
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "label": "옛날세션",
        "started_at": time.time(),
        "create_time": 1.0,          # 분명히 다른 시각
    }), encoding="utf-8")
    with L.hold(path, "새세션"):
        assert L.read(path).label == "새세션"


def test_my_own_live_lock_is_still_a_lock(tmp_path):
    """같은 프로세스가 녹화를 두 번 시작하는 것도 막아야 합니다.

    '자기 PID 면 빼앗는다' 로 두면 중첩 녹화가 그대로 통과합니다.
    """
    info = L.LockInfo(os.getpid(), "x", time.time(), L._create_time(os.getpid()))
    assert L._stale(info) is False


def test_a_corrupt_lock_file_is_replaced(tmp_path):
    path = tmp_path / "recording.lock"
    path.write_text("{깨진", encoding="utf-8")
    with L.hold(path, "새세션"):
        assert L.read(path).label == "새세션"


# --- 진짜 동시 실행 ----------------------------------------------------
def _worker(path_str, out):
    from gymhall.recorder import lock as LL

    try:
        with LL.hold(path_str, "동시"):
            time.sleep(0.4)
        out.put("got")
    except LL.RecordingBusy:
        out.put("busy")
    except Exception as exc:  # pragma: no cover
        out.put(f"error:{exc}")


def test_only_one_of_two_processes_gets_the_lock(tmp_path):
    """exists() 로 보고 쓰면 둘 다 통과합니다. O_CREAT|O_EXCL 이어야 합니다."""
    ctx = mp.get_context("spawn")
    path = str(tmp_path / "recording.lock")
    out = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(path, out)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    results = sorted(out.get() for _ in range(2))
    assert results == ["busy", "got"], results


# --- 원자적 생성과 해제 (결정적) --------------------------------------
def test_create_is_atomic_so_the_second_caller_fails(tmp_path):
    """exists() 로 보고 쓰면 두 프로세스가 둘 다 통과합니다.

    두 프로세스를 띄우는 테스트는 경쟁 창이 좁아 매번 걸리지 않습니다.
    여기서는 '이미 있으면 반드시 False' 를 직접 봅니다.
    """
    path = tmp_path / "recording.lock"
    assert L._try_create(path, "첫번째") is True
    assert L._try_create(path, "두번째") is False
    assert L.read(path).label == "첫번째", "두 번째가 덮어썼습니다"


def test_releasing_does_not_delete_someone_elses_lock(tmp_path):
    """내 잠금을 누가 빼앗아 갔으면, 나갈 때 그걸 지우면 안 됩니다."""
    path = tmp_path / "recording.lock"
    with L.hold(path, "내세션"):
        # 그사이 다른 프로세스가 잠금을 가져갔다고 칩니다.
        path.write_text(json.dumps({
            "pid": 999_999_998, "label": "남의세션",
            "started_at": time.time(), "create_time": 0.0,
        }), encoding="utf-8")
    assert path.exists(), "남의 잠금을 지웠습니다"
    assert L.read(path).label == "남의세션"


def test_a_lock_older_than_a_day_is_taken_over(tmp_path):
    """PID 가 살아 있어 보여도 하루 넘은 잠금은 빼앗습니다."""
    path = tmp_path / "recording.lock"
    path.write_text(json.dumps({
        "pid": os.getpid(), "label": "어제것",
        "started_at": time.time() - (L.STALE_SEC + 60),
        "create_time": L._create_time(os.getpid()),
    }), encoding="utf-8")
    with L.hold(path, "오늘것"):
        assert L.read(path).label == "오늘것"


def test_the_lock_is_created_with_o_excl():
    """경쟁은 단일 프로세스 테스트로 재현되지 않습니다.

    exists() 로 보고 쓰는 코드도 '두 번째 호출은 False' 는 만족합니다.
    둘이 **동시에** 들어왔을 때만 갈리는데, 그 창은 좁아서 프로세스 두
    개를 띄워도 매번 걸리지는 않습니다. 그래서 동작 대신 수단을
    고정합니다 — 커널이 하나만 통과시키게 해야 합니다.
    """
    import inspect
    import re

    src = inspect.getsource(L._try_create)
    # 함정을 설명하는 docstring 이 스스로 걸리지 않게 뺍니다.
    body = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert "O_EXCL" in body, "O_CREAT|O_EXCL 로 만들어야 합니다"
    assert "exists()" not in body, "exists() 로 보고 쓰면 둘 다 통과합니다"
