"""실패·오류를 빠짐없이 GitHub 이슈로(src/syncnote/incidents.py).

사용자 요청: 병합 충돌·녹음·녹화 오류·실패를 단 하나도 빠짐없이 이슈로. 로그 유형 구분을 철저히,
세부적으로, 정규화. 전제: 강의실 PC 와 닿는 길이 GitHub 이슈·PR·레포뿐.
분류 사례는 2026-11-26 강의실 PC(HUM-4F-1436·HUM-2F-1244) 로그의 실제 문장입니다.
"""

from __future__ import annotations

import json
import logging
import types

import pytest

from syncnote import incidents as I


def rec(msg, level=logging.ERROR, name="syncnote.recorder.audio", **extra):
    r = logging.LogRecord(name, level, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(r, k, v)
    return r


# --- 분류 ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,level,msg,code", [
    ("syncnote.recorder.audio", logging.ERROR, "[stereomix] 녹음 중 오류", "REC.AUDIO.CAPTURE"),
    ("syncnote.recorder.session", logging.ERROR, "합치기 실패: ffmpeg 실패 (code 4294967268):", "REC.MUX.FAILED"),
    ("syncnote.recorder.recover", logging.WARNING,
     "녹화 일지를 쓰지 못했습니다(꺼지면 복구가 덜 정확해짐): [Errno 28] No space left on device", "SYS.DISK.FULL"),
    ("syncnote.cli", logging.WARNING, "녹화를 시작하지 않습니다: 이미 녹화 중입니다", "REC.LOCK.BUSY"),
    ("syncnote.recorder.session", logging.ERROR,
     "[중요] 기본 녹음 장치가 '스테레오 믹스(Realtek(R) Audio)' 이고 마이크 입력이 없습니다.", "REC.MIC.MIX_ONLY"),
    ("syncnote.recorder.session", logging.ERROR,
     "화면 ffmpeg 가 예기치 않게 종료됐습니다. 이 세그먼트를 마무리하고", "REC.VIDEO.EXIT"),
    ("syncnote.recorder.video", logging.WARNING, "영상 파일이 사실상 비어 있습니다(0바이트).", "REC.VIDEO.EMPTY"),
    ("syncnote.recorder.audio", logging.WARNING, "[system] 전 구간 무음입니다. 잡은 장치는", "REC.AUDIO.SILENT"),
    ("syncnote.recorder.session", logging.ERROR, "세그먼트 3 실패 — 잠시 뒤 다음 세그먼트로", "REC.SEGMENT.FAILED"),
    ("syncnote.cli", logging.ERROR, "업데이트: failed — 받기 실패(되돌림): CONFLICT (content)", "UPDATE.MERGE_CONFLICT"),
    ("syncnote.cli", logging.ERROR, "예기치 못한 오류: boom", "SYS.UNCAUGHT"),
    ("syncnote.tasks", logging.ERROR, "작업 스케줄러 자동 등록 실패(…)", "TASKS.REGISTER_FAILED"),
    ("syncnote.stream.live", logging.WARNING, "복구 실패 (3번째). 20초 뒤 다시 시도합니다.", "STREAM.RECOVERY_FAILED"),
    ("syncnote.eclass.crawler", logging.ERROR, "과목 수집 중 오류", "ECLASS.FAILED"),
])
def test_real_log_lines_get_normalized_codes(name, level, msg, code):
    assert I.classify(rec(msg, level, name))[0] == code


def test_explicit_code_wins():
    got = I.classify(rec("업데이트: failed — 뭔가", name="syncnote.cli", incident="UPDATE.PIP_FAILED"))
    assert got[0] == "UPDATE.PIP_FAILED" and got[1] == "error"


def test_nothing_is_dropped_unknown_errors_are_unclassified():
    assert I.classify(rec("처음 보는 오류", name="syncnote.foo.bar"))[0] == "UNCLASSIFIED.FOO_BAR"


def test_plain_warnings_and_info_are_not_incidents():
    assert I.classify(rec("github.tag_strategy 는 더 이상 쓰지 않습니다", logging.WARNING, "syncnote.cli")) is None
    assert I.classify(rec("녹화 시작: 자료구조", logging.INFO, "syncnote.recorder.session")) is None


def test_its_own_logs_are_never_reported_again():
    assert I.classify(rec("올리지 못함", name="syncnote.incidents")) is None


def test_every_rule_has_a_dotted_code_and_known_severity():
    for r in I.RULES:
        assert r.code.count(".") >= 1 and r.code == r.code.upper()
        assert r.severity in ("critical", "error", "warning") and r.title


# --- 가리기·정규화 -------------------------------------------------------------------

def test_redact():
    text = (r"C:\Users\1121\AppData\Local\sync-note 로그인 시도: 2601057 token=ghp_abcdefghijklmnop1234 "
            "from 100.105.112.32 a@b.com 이름: 홍길동")
    out = I.redact(text)
    for gone in ("1121", "2601057", "ghp_abcdef", "100.105.112.32", "a@b.com", "홍길동"):
        assert gone not in out, gone
    assert "<사용자>" in out and "<번호>" in out and "<IP>" in out


def test_normalize_groups_similar_messages():
    a = I.normalize(r"세그먼트 3 실패: 'C:\x\a.wav' 12.5초")
    b = I.normalize(r"세그먼트 7 실패: 'D:\y\b.wav' 3초")
    assert a == b


# --- 잡기(핸들러) ---------------------------------------------------------------------

def test_handler_queues_with_context_and_exception(tmp_path):
    h = I.IncidentHandler(tmp_path / "q.jsonl", "HUM-4F-1436")
    lg = logging.getLogger("syncnote.test_incident_ctx")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.info("녹화 시작: 자료구조")
        try:
            raise OSError(28, "No space left on device")
        except OSError:
            lg.exception("[stereomix] 녹음 중 오류")
    finally:
        lg.removeHandler(h)
    items = [json.loads(x) for x in (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 1
    it = items[0]
    assert it["code"] == "REC.AUDIO.CAPTURE" and it["machine"] == "HUM-4F-1436"
    assert "No space left" in it["exception"] and any("녹화 시작" in c for c in it["context"])


def test_handler_never_raises(tmp_path):
    bad = tmp_path / "file.txt"
    bad.write_text("x", encoding="utf-8")
    h = I.IncidentHandler(bad / "q.jsonl", "pc")          # 부모가 파일이라 못 만듦
    h.emit(rec("[mic] 녹음 중 오류"))                        # 예외 없이 지나감


# --- 올리기 ---------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self):
        self.issues, self.comments, self.closed = {}, [], set()
        self.n = 0
        self.name = "fake"

    def create(self, title, body, labels):
        self.n += 1
        self.issues[self.n] = {"title": title, "body": body, "labels": labels}
        return self.n

    def comment(self, number, body):
        self.comments.append((number, body))

    def is_open(self, number):
        return number not in self.closed


def queue(rep, *items):
    rep.dir.mkdir(parents=True, exist_ok=True)
    with rep.queue.open("a", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")


def item(code="REC.AUDIO.CAPTURE", machine="HUM-4F-1436", msg="[stereomix] 녹음 중 오류", at=1000.0):
    return {"at": at, "machine": machine, "host": "h", "code": code, "severity": "critical",
            "title": "녹음 중 오류", "logger": "syncnote.recorder.audio", "level": "ERROR",
            "message": msg, "exception": "", "context": ["15:12:07 [INFO] x: 녹화 중"], "command": "record watch",
            "pid": 1, "version": "abc1234"}


def test_one_issue_per_pc_and_code_then_comments(tmp_path):
    gh = FakeGitHub()
    rep = I.Reporter(tmp_path, lambda: gh, min_comment_minutes=10)
    queue(rep, item(), item(at=1001), item(code="REC.MUX.FAILED", msg="합치기 실패"))
    r = rep.flush(now=2000)
    assert sorted(r.created) == [1, 2] and r.kept == 0
    assert gh.issues[1]["title"].startswith("[HUM-4F-1436] REC.AUDIO.CAPTURE")
    assert "sev:critical" in gh.issues[1]["labels"] and "pc:HUM-4F-1436" in gh.issues[1]["labels"]
    assert gh.comments and gh.comments[0][0] == 1                     # 같은 묶음의 두 번째 발생
    # 10분 안에 또 나면 모았다가
    queue(rep, item(at=2100))
    r = rep.flush(now=2100)
    assert r.kept == 1 and not r.commented
    # 10분이 지나면 그 이슈에 댓글
    r = rep.flush(now=2000 + 601)
    assert r.commented == [1] and r.kept == 0


def test_closed_issue_opens_a_new_one(tmp_path):
    gh = FakeGitHub()
    rep = I.Reporter(tmp_path, lambda: gh)
    queue(rep, item())
    rep.flush(now=1000)
    gh.closed.add(1)
    queue(rep, item(at=5000))
    r = rep.flush(now=5000)
    assert r.created == [2]


def test_offline_or_no_credentials_keeps_everything(tmp_path):
    rep = I.Reporter(tmp_path, lambda: None)
    queue(rep, item(), item(code="SYS.DISK.FULL"))
    r = rep.flush()
    assert r.kept == 2 and "길이 없음" in r.error
    assert len(rep._read_queue()) == 2

    class Broken(FakeGitHub):
        def create(self, *a):
            raise ConnectionError("네트워크 끊김")
    r = I.Reporter(tmp_path, lambda: Broken()).flush()
    assert "네트워크" in r.error and len(rep._read_queue()) == 2       # 잃지 않음


def test_write_cap_leaves_the_rest_for_next_time(tmp_path):
    gh = FakeGitHub()
    rep = I.Reporter(tmp_path, lambda: gh, max_writes=2)
    queue(rep, *[item(code=f"UNCLASSIFIED.X{i}") for i in range(5)])
    r = rep.flush()
    assert len(r.created) == 2 and r.kept == 3


def test_issue_body_is_redacted(tmp_path):
    gh = FakeGitHub()
    rep = I.Reporter(tmp_path, lambda: gh)
    queue(rep, item(msg=r"C:\Users\1121\x 로그인 2601057 실패"))
    rep.flush()
    body = gh.issues[1]["body"]
    assert "1121" not in body and "2601057" not in body and "REC.AUDIO.CAPTURE" in body


# --- 설치·연결 -------------------------------------------------------------------------

@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNCNOTE_NO_INCIDENTS", raising=False)
    monkeypatch.setattr(I, "_installed", {})
    monkeypatch.setattr(I, "_install_hooks", lambda: None)
    cfg = types.SimpleNamespace(get=lambda k, d=None: d, machine_label="PC1")
    layout = types.SimpleNamespace(state=tmp_path / "state")
    rep = I.install(cfg, layout, background=False)
    yield rep
    lg = logging.getLogger("syncnote")
    for h in list(lg.handlers):
        if isinstance(h, I.IncidentHandler):
            lg.removeHandler(h)


def test_install_catches_every_syncnote_error(installed):
    logging.getLogger("syncnote.recorder.session").error("합치기 실패: x")
    logging.getLogger("syncnote.whatever").error("처음 보는 것")
    codes = [i["code"] for i in installed._read_queue()]
    assert codes == ["REC.MUX.FAILED", "UNCLASSIFIED.WHATEVER"]


def test_update_failures_carry_exact_codes(monkeypatch, installed):
    from syncnote import cli, updater

    monkeypatch.setattr(cli, "_load", lambda a: (types.SimpleNamespace(get=lambda k, d=None: d),
                                                 types.SimpleNamespace(state=installed.dir.parent)))
    monkeypatch.setattr(updater, "update", lambda **kw: updater.Result(
        "failed", "받기 실패(되돌림): CONFLICT (content): Merge conflict in src/x.py", pip="failed"))
    assert cli.main(["update"]) == 1
    codes = [i["code"] for i in installed._read_queue()]
    assert "UPDATE.MERGE_CONFLICT" in codes and "UPDATE.PIP_FAILED" in codes


def test_setup_installs_git_and_gh_and_doctor_checks_them():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    setup = (root / "scripts" / "setup.bat").read_text(encoding="ascii")
    assert "call :winget_install Git.Git" in setup and "call :winget_install GitHub.cli" in setup
    cli_src = (root / "src" / "syncnote" / "cli.py").read_text(encoding="utf-8")
    assert '("git", "자동 업데이트' in cli_src and '("gh", "오류 이슈 보고' in cli_src


@pytest.mark.parametrize("name,msg,code", [
    ("syncnote.cli", "녹화 복구 중 오류(녹화 대기는 계속합니다)", "RECOVER.FAILED"),
    ("syncnote.recorder.recover", "녹화 조각 복구 실패: 20261126_151344_part005", "RECOVER.FAILED"),
    ("syncnote.stream.live", "복구 실패 (3번째). 20초 뒤 다시 시도합니다.", "STREAM.RECOVERY_FAILED"),
])
def test_recording_recovery_and_stream_recovery_are_told_apart(name, msg, code):
    assert I.classify(rec(msg, logging.ERROR, name))[0] == code
