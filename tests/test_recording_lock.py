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

from syncnote.recorder import lock as L


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
#
# 처음에 두 프로세스를 동시에 띄우고 sleep 으로 겹치기를 바랐는데,
# CI 에서 네 잡 모두 ['got', 'got'] 이 나왔습니다. spawn 으로 파이썬을
# 새로 띄우는 데 걸리는 시간이 들쭉날쭉해서, 먼저 뜬 쪽이 잠금을 잡고
# **놓고 나간 뒤** 두 번째가 떴기 때문입니다. 겹치지 않으면 둘 다
# 성공하는 것이 맞습니다 — 코드가 아니라 테스트가 틀렸습니다.
#
# 이제 신호로 겹치기를 보장합니다. A 가 잠금을 잡았다고 알린 뒤에야
# B 가 시도하고, B 가 끝날 때까지 A 는 놓지 않습니다.
def _holder(path_str, held, release, out):
    from syncnote.recorder import lock as LL

    try:
        with LL.hold(path_str, "먼저"):
            held.set()
            release.wait(20)
        out.put("got")
    except LL.RecordingBusy:
        out.put("busy")
    except Exception as exc:  # pragma: no cover
        out.put(f"error:{exc}")


def _challenger(path_str, held, release, out):
    from syncnote.recorder import lock as LL

    held.wait(20)                      # 상대가 확실히 들고 있을 때 시도
    try:
        with LL.hold(path_str, "나중"):
            pass
        out.put("got")
    except LL.RecordingBusy:
        out.put("busy")
    except Exception as exc:  # pragma: no cover
        out.put(f"error:{exc}")
    finally:
        release.set()


def test_the_second_process_is_refused_while_the_first_holds(tmp_path):
    ctx = mp.get_context("spawn")
    path = str(tmp_path / "recording.lock")
    held, release, out = ctx.Event(), ctx.Event(), ctx.Queue()
    procs = [
        ctx.Process(target=_holder, args=(path, held, release, out)),
        ctx.Process(target=_challenger, args=(path, held, release, out)),
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    release.set()
    results = sorted(out.get(timeout=10) for _ in range(2))
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
