"""'조용한 0' — 아무것도 못 했는데 성공으로 끝나는 자리.

2026-11-12: 업로드 0건인데 종료 코드 0, CI 초록불. 파일 16개 중 0개가
올라갔습니다. `_upload_eclass` 를 고쳤는데, 변이 테스트 범위를 "최근
PR 변경분" 으로 잡는 바람에 **같은 모양이 다른 곳에 그대로 남아
있었습니다.** `cmd_upload` 가 그랬습니다.

그래서 변이 세트의 단위를 바꿉니다. "PR 변경분" 이 아니라 **"같은
실패 패턴"** 입니다. 버그를 하나 찾으면 그 모양으로 전 코드를 훑습니다
(docs/TESTING.md 원칙 7).

훑어서 나온 것 셋:
  cmd_transcribe_queue  큐 20건을 전부 실패해도 run_queue 가 [] 를
                        돌려주고 종료 코드 0. 야간 무인 작업이 조용히
                        성공으로 기록됩니다.
  cmd_transcribe_queue  전사 결과 업로드에 tally 가 없었습니다.
  cmd_record_start      다른 녹화가 돌고 있으면 _run_recording 이
                        None 을 돌려주는데, 그걸 버리고 0 을 돌려줬습니다.
                        한 프레임도 안 찍고 '성공' 입니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from syncnote import cli
from syncnote.paths import Layout


class Cfg:
    machine_label = "pc"

    def __init__(self, d=None):
        self._d = d or {}
        self.data_dir = Path("/tmp")

    def get(self, key, default=None):
        return self._d.get(key, default)

    def section(self, _k):
        return {}


class _Args:
    limit = 0
    language = None
    show = False
    no_upload = True
    dry_run = True
    label = "수동"
    minutes = 0.01
    course = ""


# ---------------------------------------------------------------------
# 전사 대기열
# ---------------------------------------------------------------------
@pytest.fixture
def queue_cmd(tmp_path, monkeypatch):
    state = {"results": [], "tally": {}}

    def _fake_run_queue(cfg, layout, *, limit=0, language=None, tally=None):
        if tally is not None:
            tally.update(state["tally"])
        return state["results"]

    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"github.enabled": False}), Layout(tmp_path)))
    import syncnote.transcribe.batch as batch

    monkeypatch.setattr(batch, "run_queue", _fake_run_queue)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    return (lambda: cli.cmd_transcribe_queue(_Args())), state


def test_an_empty_queue_is_success(queue_cmd):
    run, state = queue_cmd
    state["tally"] = {"attempted": 0, "succeeded": 0, "failed": 0, "missing": 0}
    assert run() == 0


def test_a_queue_that_failed_entirely_is_not_success(queue_cmd, capsys):
    """밤새 20건을 돌렸는데 전부 실패. 조용히 끝나면 안 됩니다."""
    run, state = queue_cmd
    state["tally"] = {"attempted": 20, "succeeded": 0, "failed": 20, "missing": 0}
    assert run() == 1, "큐 20건을 전부 실패했는데 성공으로 끝났습니다"
    assert "::error" in capsys.readouterr().out


def test_a_partial_failure_is_not_success(queue_cmd):
    """일부만 됐으면 나머지는 대기열에 남습니다. 알려야 합니다."""
    run, state = queue_cmd

    class _R:
        audio = Path("a.wav")
        speed = 2.0
        outputs: list = []

    state["results"] = [_R()]
    state["tally"] = {"attempted": 3, "succeeded": 1, "failed": 2, "missing": 0}
    assert run() == 1


def test_a_vanished_file_is_also_reported(queue_cmd):
    run, state = queue_cmd
    state["tally"] = {"attempted": 0, "succeeded": 0, "failed": 0, "missing": 2}
    assert run() == 1


# ---------------------------------------------------------------------
# run_queue 의 집계 자체
# ---------------------------------------------------------------------
def test_run_queue_separates_an_empty_queue_from_total_failure(tmp_path, monkeypatch):
    """돌려주는 리스트만 보면 둘이 똑같이 빈 리스트입니다."""
    import syncnote.transcribe.batch as batch

    layout = Layout(tmp_path)
    layout.state.mkdir(parents=True, exist_ok=True)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")

    q = batch.Queue(layout.state / batch.QUEUE_FILE)
    q.add(audio, label="수업", out_dir=tmp_path, track="system")

    monkeypatch.setattr(batch.EngineSpec, "load", lambda self: object())
    monkeypatch.setattr(batch, "transcribe_file",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("모델 없음")))

    tally: dict[str, int] = {}
    got = batch.run_queue(Cfg(), layout, tally=tally)
    assert got == []
    assert tally["attempted"] == 1 and tally["failed"] == 1
    assert tally["succeeded"] == 0


def test_run_queue_reports_nothing_attempted_for_an_empty_queue(tmp_path):
    import syncnote.transcribe.batch as batch

    layout = Layout(tmp_path)
    layout.state.mkdir(parents=True, exist_ok=True)
    tally: dict[str, int] = {}
    assert batch.run_queue(Cfg(), layout, tally=tally) == []
    assert tally["attempted"] == 0 and tally["failed"] == 0


# ---------------------------------------------------------------------
# 녹화 잠금 충돌
# ---------------------------------------------------------------------
def test_record_start_fails_when_another_recording_holds_the_lock(
        tmp_path, monkeypatch, caplog):
    """한 프레임도 안 찍고 '성공' 으로 끝나면 안 됩니다."""
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: None)
    with caplog.at_level("ERROR", logger="syncnote"):
        assert cli.cmd_record_start(_Args()) == 1
    assert "하나도 하지 못했습니다" in caplog.text


def test_record_start_succeeds_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: object())
    assert cli.cmd_record_start(_Args()) == 0


# ---------------------------------------------------------------------
# 같은 모양이 또 생기는 것을 막습니다
# ---------------------------------------------------------------------
# cmd_upload 를 찾은 방법이 "PR 변경분 변이" 가 아니라 "같은 모양을
# 손으로 grep" 이었습니다. 그 grep 을 테스트로 고정합니다.
#
# 규칙: upload_many / run_queue 처럼 **성공한 것만 돌려주는** 함수를
# 부르면서 tally 를 안 받으면, 호출부에서 "전부 실패" 와 "할 일 없음"
# 을 구분할 수 없습니다.
# 성공한 것만 돌려주는 함수들. 여기 빠지면 검사가 그 함수를 안 봅니다.
# 손으로 관리하면 새 집계 함수를 만들 때 넣는 걸 잊습니다 — 그게 바로
# "변이 세트를 PR 변경분으로 잡아서 cmd_upload 를 놓친" 그 문제입니다.
# 아래 test_watched_covers_... 가 그 망각을 막습니다.
WATCHED = frozenset({"upload_many", "run_queue"})


def test_every_partial_result_call_asks_for_a_tally():
    """성공만 돌려주는 함수는 tally 없이 부르면 안 됩니다."""
    import ast

    src = Path(cli.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"), str(src))
    watched = WATCHED
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
        if name in watched and not any(k.arg == "tally" for k in node.keywords):
            bad.append(f"cli.py:{node.lineno} {name}()")
    assert not bad, (
        "성공한 것만 돌려주는 함수를 tally 없이 불렀습니다. 호출부에서 "
        "'전부 실패' 와 '할 일 없음' 이 구분되지 않습니다:\n  "
        + "\n  ".join(bad)
    )


def test_the_guard_catches_a_tally_less_call(tmp_path):
    """검사기가 아무것도 못 잡으면 위 테스트는 영원히 초록입니다."""
    import ast

    sample = tmp_path / "x.py"
    sample.write_text(
        "upload_many(u, files, tag, store=s)\n"          # 위반
        "upload_many(u, files, tag, tally=t)\n"          # 정상
        "run_queue(cfg, layout)\n"                       # 위반
        "other_call(x)\n",                               # 무관
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    watched = {"upload_many", "run_queue"}
    bad = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")) in watched
        and not any(k.arg == "tally" for k in n.keywords)
    ]
    assert bad == [1, 3], bad


def test_run_queue_counts_a_vanished_file(tmp_path, monkeypatch):
    """대기열에 있는데 파일이 지워진 경우. 가짜 tally 로는 안 잡힙니다."""
    import syncnote.transcribe.batch as batch

    layout = Layout(tmp_path)
    layout.state.mkdir(parents=True, exist_ok=True)
    gone = tmp_path / "없어진.wav"
    gone.write_bytes(b"RIFF")

    q = batch.Queue(layout.state / batch.QUEUE_FILE)
    q.add(gone, label="수업", out_dir=tmp_path, track="system")
    gone.unlink()

    monkeypatch.setattr(batch.EngineSpec, "load", lambda self: object())
    tally: dict[str, int] = {}
    assert batch.run_queue(Cfg(), layout, tally=tally) == []
    assert tally["missing"] == 1, tally
    assert tally["attempted"] == 0, "없는 파일을 시도로 세면 안 됩니다"


# --- 전사 결과 업로드 ---------------------------------------------------
@pytest.fixture
def queue_with_upload(tmp_path, monkeypatch):
    """전사는 성공했는데 그 결과를 못 올리는 경우."""
    import syncnote.transcribe.batch as batch
    from syncnote import release as R

    out = tmp_path / "t.txt"
    out.write_text("x", encoding="utf-8")

    class _R:
        audio = Path("a.wav")
        speed = 2.0
        outputs = [out]

    def _fake_run_queue(cfg, layout, *, limit=0, language=None, tally=None):
        if tally is not None:
            tally.update({"attempted": 1, "succeeded": 1,
                          "failed": 0, "missing": 0})
        return [_R()]

    monkeypatch.setattr(batch, "run_queue", _fake_run_queue)
    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"github.enabled": True}), Layout(tmp_path)))

    state = {"fail": True}

    class FakeUploader:
        slug = "owner/repo"

        def upload(self, path, tag, *, prefix="", rel_path=None):
            if state["fail"]:
                raise R.ReleaseError("403")
            return {"assets": [{"name": Path(path).name}]}

    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: FakeUploader())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    class _A(_Args):
        no_upload = False

    return (lambda: cli.cmd_transcribe_queue(_A())), state


def test_transcribed_but_not_uploaded_is_not_success(queue_with_upload, capsys):
    """전사는 됐는데 결과가 안 올라갔습니다. 조용하면 안 됩니다."""
    run, state = queue_with_upload
    state["fail"] = True
    assert run() == 1, "전사 결과를 하나도 못 올렸는데 성공으로 끝났습니다"
    assert "::error" in capsys.readouterr().out


def test_transcribed_and_uploaded_is_success(queue_with_upload, capsys):
    run, state = queue_with_upload
    state["fail"] = False
    assert run() == 0
    assert "::error" not in capsys.readouterr().out


def test_watched_covers_every_function_that_takes_a_tally():
    """tally 를 받는 함수는 전부 WATCHED 에 있어야 합니다.

    WATCHED 를 손으로 관리하면 새 집계 함수를 만들 때 넣는 걸 잊습니다.
    그러면 위 검사가 그 함수를 안 보고, 호출부가 tally 없이 불러도
    조용합니다 — 이번 라운드에 cmd_upload 를 놓친 것과 똑같은 모양이
    됩니다. 원칙 7 을 사람의 기억이 아니라 구조에 맡깁니다.
    """
    import ast

    from syncnote import release
    from syncnote.transcribe import batch

    takes_tally = set()
    for module in (release, batch):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = [a.arg for a in (*args.args, *args.posonlyargs, *args.kwonlyargs)]
            if "tally" in names:
                takes_tally.add(node.name)

    assert takes_tally, "tally 를 받는 함수를 하나도 못 찾았습니다 — 검사기가 고장났습니다"
    missing = takes_tally - WATCHED
    assert not missing, (
        f"WATCHED 에 없는 집계 함수: {sorted(missing)}\n"
        "새 집계 함수를 만들었으면 WATCHED 에 추가하고, 호출부에서 "
        "tally 를 넘기세요."
    )


def test_watched_has_no_stale_names():
    """지워진 함수가 WATCHED 에 남아 있으면 검사가 느슨해집니다."""
    from syncnote import release
    from syncnote.transcribe import batch

    known = {n for m in (release, batch) for n in dir(m)}
    stale = WATCHED - known
    assert not stale, f"이제 없는 함수가 WATCHED 에 남아 있습니다: {sorted(stale)}"


# ---------------------------------------------------------------------
# _run_recording 호출부는 셋입니다. 하나만 고치면 반쪽입니다.
# ---------------------------------------------------------------------
def test_every_run_recording_caller_checks_the_result():
    """잠금 충돌 때 None 이 오는데, 버리면 조용히 유실됩니다."""
    import ast

    from syncnote import cli as cli_mod

    src = Path(cli_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("cmd_"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_run_recording"):
                # 반환값을 변수에 담지 않으면 Expr 문입니다 = 버린 것.
                stmts = [n for n in ast.walk(fn)
                         if isinstance(n, ast.Expr) and n.value is node]
                callers.append((fn.name, bool(stmts)))
    assert callers, "_run_recording 호출부를 못 찾았습니다"
    discarded = [name for name, thrown_away in callers if thrown_away]
    assert not discarded, (
        f"_run_recording 의 반환값을 버린 곳: {discarded}. "
        "잠금 충돌이면 None 이 오고, 그걸 버리면 한 프레임도 못 찍고 "
        "'성공' 으로 끝납니다."
    )
    assert len(callers) >= 3, f"호출부가 {len(callers)}개뿐입니다 — 검사기 확인 필요"


def test_schedule_logs_an_error_when_a_class_is_lost(tmp_path, monkeypatch, caplog):
    """상시 실행이라 종료 코드가 아니라 로그가 신호입니다."""
    from datetime import datetime, timedelta

    from syncnote.recorder import schedule as sched

    class _Slot:
        name = "자료구조"
        language = None
        course_id = ""

    nxt = sched.Occurrence(_Slot(), datetime.now(),
                           datetime.now() + timedelta(minutes=50))
    monkeypatch.setattr(sched, "parse", lambda _t: [_Slot()])
    monkeypatch.setattr(sched, "describe", lambda _s: "")
    monkeypatch.setattr(sched, "next_occurrence", lambda *a, **kw: nxt)
    monkeypatch.setattr(sched, "wait_until", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"recording.timetable": [{}]}), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: None)

    class _A(_Args):
        once = True

    with caplog.at_level("ERROR", logger="syncnote"):
        rc = cli.cmd_record_schedule(_A())
    assert rc == 1, "--once 인데 녹화를 못 하고도 0 을 돌려줬습니다"
    assert "자료구조" in caplog.text and "녹화되지 않았습니다" in caplog.text


def test_watch_once_reports_a_failed_capture(tmp_path, monkeypatch):
    """감지는 했는데 녹화를 못 했습니다. KeyboardInterrupt 로 숨으면 안 됩니다."""
    from syncnote.recorder import watcher as w

    class _FakeWatcher:
        def __init__(self, **kw):
            pass

        def watch(self, on_start):
            on_start("zoom.exe")

        def wait_until_gone(self, *a, **kw):
            return True

    monkeypatch.setattr(w, "Watcher", _FakeWatcher)
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: None)

    class _A(_Args):
        once = True

    assert cli.cmd_record_watch(_A()) == 1


def test_watch_returns_zero_when_recording_works(tmp_path, monkeypatch):
    from syncnote.recorder import watcher as w

    class _FakeWatcher:
        def __init__(self, **kw):
            pass

        def watch(self, on_start):
            on_start("zoom.exe")

        def wait_until_gone(self, *a, **kw):
            return True

    monkeypatch.setattr(w, "Watcher", _FakeWatcher)
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: object())

    class _A(_Args):
        once = True

    assert cli.cmd_record_watch(_A()) == 0


def test_watch_once_failure_is_not_the_same_as_a_user_interrupt(tmp_path, monkeypatch):
    """스케줄 작업으로 걸면 둘을 구분해야 합니다.

    예전에는 --once 가 KeyboardInterrupt 를 던져 바깥의 except 가
    사용자의 진짜 Ctrl+C 와 섞었습니다.
    """
    from syncnote.recorder import watcher as w

    class _FakeWatcher:
        def __init__(self, **kw):
            pass

        def watch(self, on_start):
            on_start("zoom.exe")

        def wait_until_gone(self, *a, **kw):
            return True

    monkeypatch.setattr(w, "Watcher", _FakeWatcher)
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_run_recording", lambda *a, **kw: None)

    class _A(_Args):
        once = True

    assert cli.cmd_record_watch(_A()) == 1, "녹화 실패인데 1 이 아닙니다"


def test_a_real_ctrl_c_during_watch_is_130(tmp_path, monkeypatch):
    """사용자 중단은 main 이 130 으로 내보냅니다. 1 로 바꾸면 안 됩니다."""
    from syncnote.recorder import watcher as w

    class _InterruptedWatcher:
        def __init__(self, **kw):
            pass

        def watch(self, on_start):
            raise KeyboardInterrupt

    monkeypatch.setattr(w, "Watcher", _InterruptedWatcher)
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))

    class _A(_Args):
        once = False

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_record_watch(_A())
