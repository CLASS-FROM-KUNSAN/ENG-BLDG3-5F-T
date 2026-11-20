"""eClass 과목 목록 받기: probe → (비밀번호 없으면) set_password 1 → 다시 probe."""

import os
import sys
import textwrap
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from syncnote.eclass import catalog as CAT  # noqa: E402
from syncnote.timetable import eclass_fetch as F  # noqa: E402
from syncnote.timetable import gui  # noqa: E402
from syncnote.timetable import model as M  # noqa: E402

FAKE_PROBE = textwrap.dedent('''
    import sys, os, time, json
    marker, state, pidfile = sys.argv[1], sys.argv[2], sys.argv[3]
    print("eclass probe 시작", flush=True)
    if os.path.exists(marker):
        os.makedirs(state, exist_ok=True)
        data = {"version": 1, "terms": {"2026-2학기": {"seen_at": "2026-11-21T10:00:00",
                "courses": [{"id": "C001", "name": "운동과건강"}, {"id": "C002", "name": "자료구조"}]}}}
        with open(os.path.join(state, "eclass_courses.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("과목 목록 2개를 남겼습니다: " + state, flush=True)
    else:
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
        print("Eclass 로그인 비밀번호 을(를) 찾을 수 없습니다.", flush=True)
        print("  저장:   python -m syncnote secrets set eclass", flush=True)
        time.sleep(60)          # 브라우저가 떠 있는 것처럼 — 편집기가 닫아야 함
''')
FAKE_PW = "import sys; open(sys.argv[1], 'w').write('ok')\n"


@pytest.fixture(scope="module")
def app(qt_app):
    return qt_app        # 세션 하나짜리(conftest) — 파일마다 만들면 정리 순서로 죽음


@pytest.fixture
def fake(tmp_path, monkeypatch):
    probe = tmp_path / "fake_probe.py"
    probe.write_text(FAKE_PROBE, encoding="utf-8")
    pw = tmp_path / "fake_pw.py"
    pw.write_text(FAKE_PW, encoding="utf-8")
    marker, state, pidfile = tmp_path / "pw_set", tmp_path / "data" / "state", tmp_path / "pid"
    monkeypatch.setattr(F.CourseListFetcher, "probe_command",
                        lambda self: [sys.executable, str(probe), str(marker), str(state), str(pidfile)])
    monkeypatch.setattr(F.CourseListFetcher, "password_command",
                        lambda self: [sys.executable, str(pw), str(marker)])
    monkeypatch.setattr(F, "NEW_CONSOLE", 0)          # 테스트에서 콘솔 창을 띄우지 않음
    return {"marker": marker, "state": state, "pidfile": pidfile, "pw": pw}


def _run(app, dlg, seconds=30):
    dlg.show()
    dlg.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end and dlg.isVisible() and "⚠" not in dlg.status.text():
        app.processEvents()
        time.sleep(0.02)


def _alive(pid):
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace").stdout
    return str(pid) in out


def test_password_hint_closes_probe_sets_password_then_retries(app, tmp_path, fake):
    dlg = F.CourseListFetcher(None, tmp_path / "config.toml")
    _run(app, dlg)
    log = dlg.log.toPlainText()
    assert dlg.result() == QDialog.DialogCode.Accepted, log
    assert "비밀번호 저장 창을 엽니다" in log and "다시 가져옵니다" in log
    assert dlg.password_rounds == 1 and dlg.saved == 2
    assert fake["marker"].exists()                                  # 비밀번호 단계가 돌았음
    pid = int(fake["pidfile"].read_text(encoding="utf-8"))
    if os.name == "nt":
        assert not _alive(pid), "비밀번호를 요구한 probe 가 닫히지 않았습니다"
    assert [e.id for e in CAT.load(fake["state"])] == ["C001", "C002"]


def test_asking_for_password_again_stops_instead_of_looping(app, tmp_path, fake, monkeypatch):
    monkeypatch.setattr(F.CourseListFetcher, "password_command",
                        lambda self: [sys.executable, "-c", "pass"])   # 저장 안 됨
    dlg = F.CourseListFetcher(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert "다시 비밀번호를 요구" in dlg.status.text() and dlg.stop_btn.text() == "닫기"
    dlg.close()


def test_probe_without_course_list_reports_failure(app, tmp_path, monkeypatch):
    monkeypatch.setattr(F.CourseListFetcher, "probe_command",
                        lambda self: [sys.executable, "-c", "print('로그인 실패: 학번이 비었습니다')"])
    dlg = F.CourseListFetcher(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert "받지 못했습니다" in dlg.status.text()
    assert "학번이 비었습니다" in dlg.log.toPlainText()
    dlg.close()


def test_probe_runs_with_the_editors_config(tmp_path):
    env = F.CourseListFetcher._env(type("X", (), {"config_path": tmp_path / "my.toml"})())
    assert env["SYNCNOTE_CONFIG"].endswith("my.toml")


def test_real_scripts_are_what_the_editor_runs():
    d = F.CourseListFetcher.__new__(F.CourseListFetcher)
    if os.name == "nt":
        assert d.probe_command()[-1].endswith("eclass_probe.bat")
        assert d.password_command()[-2:] == [str(F.SCRIPTS / "set_password.bat"), "1"]
    assert (F.SCRIPTS / "eclass_probe.bat").exists() and (F.SCRIPTS / "set_password.bat").exists()


def test_link_courses_without_catalog_fetches_instead_of_explaining(app, tmp_path, monkeypatch):
    data = tmp_path / "data"
    p = tmp_path / "config.toml"
    p.write_text(f'[general]\ndata_dir = "{data.as_posix()}"\n', encoding="utf-8")
    M.save(p, M.Timetable([M.Course("운동 건강과", ["fri"], "13:00", "14:00")]))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: pytest.fail("안내 창을 띄우면 안 됨")))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Discard)

    def fetched(self):
        CAT.save(data / "state", "2026-2학기",
                 [type("C", (), {"course_id": "C001", "name": "운동과건강"})()])
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(F.CourseListFetcher, "exec", fetched)
    monkeypatch.setattr(gui.LinkDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    w = gui.TimetableWindow(p)
    try:
        assert w.catalog == []
        w.link_courses()
        assert [e.id for e in w.catalog] == ["C001"]
        assert w.tt.courses[0].course_id == "C001" and w.tt.courses[0].name == "운동과건강"
    finally:
        w.undo.setClean()
        w.close()
