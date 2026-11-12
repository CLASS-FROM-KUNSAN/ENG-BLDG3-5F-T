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

from gymhall import cli
from gymhall.paths import Layout


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
    import gymhall.transcribe.batch as batch

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
    import gymhall.transcribe.batch as batch

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
    import gymhall.transcribe.batch as batch

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
    with caplog.at_level("ERROR", logger="gymhall"):
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
def test_every_partial_result_call_asks_for_a_tally():
    """성공만 돌려주는 함수는 tally 없이 부르면 안 됩니다."""
    import ast

    src = Path(cli.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"), str(src))
    watched = {"upload_many", "run_queue"}
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
    import gymhall.transcribe.batch as batch

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
    import gymhall.transcribe.batch as batch
    from gymhall import release as R

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
