"""백업/정리 잠금: 다른 프로세스가 쥐고 있으면 못 얻고, 죽으면 풀립니다."""

from __future__ import annotations

import subprocess
import sys
import textwrap

from syncnote.backup.lock import exclusive


def test_second_holder_in_another_process_is_refused(tmp_path):
    lock = tmp_path / "x.lock"
    with exclusive(lock) as got:
        assert got
        code = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(__import__('syncnote').__path__[0] + '/..')!r})
            from pathlib import Path
            from syncnote.backup.lock import exclusive
            with exclusive(Path({str(lock)!r})) as got:
                print("GOT" if got else "BUSY")
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, encoding="utf-8", check=True).stdout
        assert out.strip() == "BUSY"
    with exclusive(lock) as got:
        assert got


def test_lock_released_when_holder_dies(tmp_path):
    lock = tmp_path / "x.lock"
    code = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(__import__('syncnote').__path__[0] + '/..')!r})
        from pathlib import Path
        from syncnote.backup.lock import exclusive
        with exclusive(Path({str(lock)!r})) as got:
            assert got
            os._exit(0)   # 정리 없이 죽음
    """)
    subprocess.run([sys.executable, "-c", code], check=True)
    with exclusive(lock) as got:
        assert got


def test_wait_gives_up(tmp_path):
    lock = tmp_path / "x.lock"
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(__import__('syncnote').__path__[0] + '/..')!r})
        from pathlib import Path
        from syncnote.backup.lock import exclusive
        with exclusive(Path({str(lock)!r})) as got:
            print("held", flush=True)
            time.sleep(5)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                            text=True, encoding="utf-8")
    try:
        assert proc.stdout.readline().strip() == "held"
        with exclusive(lock, wait_sec=0.5, poll_sec=0.1) as got:
            assert not got
    finally:
        proc.kill()
        proc.wait()
