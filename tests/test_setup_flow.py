"""저장 뒤 설치·점검 흐름 창 + 저장 분기(config.toml 있음/없음). PySide6 필요."""

import json
import os
import sys
import textwrap
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from syncnote.timetable import (
    eclass_fetch,  # noqa: E402
    gui,  # noqa: E402
)
from syncnote.timetable import model as M  # noqa: E402
from syncnote.timetable import setup_flow as SF  # noqa: E402


@pytest.fixture(scope="module")
def app(qt_app):
    return qt_app


def _py(tmp_path, name, body):
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(f)]


DOCTOR = {"ok": False, "info": [["Python", "3.12"], ["설정 파일", "x"]], "config_error": None,
          "sections": [{"title": "파이썬 패키지", "items": [
              {"mark": "O", "name": "requests", "detail": "GitHub 업로드"},
              {"mark": "X", "name": "soundcard", "detail": "시스템 소리 캡처 — pip install soundcard"}]}],
          "verdict": "위의 [X] 항목을 해결하세요."}
DEVICES = {"audio_error": None, "monitors": [{"index": 1, "text": "1: 1920x1080", "primary": True}],
           "groups": [{"kind": "loopback", "title": "시스템 소리 캡처용",
                       "config_key": "recording.audio.system_device",
                       "devices": [{"name": "스피커(Realtek)", "default": True}]},
                      {"kind": "microphone", "title": "마이크",
                       "config_key": "recording.audio.mic_device",
                       "devices": [{"name": "마이크 배열", "default": True}]},
                      {"kind": "speaker", "title": "재생 장치", "config_key": "", "devices": []}]}


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    m = tmp_path / "m"
    m.mkdir()
    cmds = {
        "setup": _py(tmp_path, "setup", f"""
            open(r"{m / 'venv'}", "w").write("ok")          # .venv 가 생긴 것처럼
            open(r"{m / 'setup_ran'}", "a").write("x")
            print("[1/5] 가상환경 만드는 중...")
            print("[2/5] 파이썬 패키지 설치 중...")
            print(" 설치 완료. 이제 다음 순서로 진행하세요.")
        """),
        "state": _py(tmp_path, "state", f"""
            import json, os
            e = lambda n: os.path.exists(os.path.join(r"{m}", n))
            print(json.dumps({{"python": "x", "in_venv": e("venv"),
                              "reqs_bad": [] if e("installed") else ["psutil"],
                              "ffmpeg": None if e("no_ffmpeg") else "ffmpeg.exe",
                              "chromium": True}}))
        """),
        "reqcheck": _py(tmp_path, "reqcheck", f"""
            import json, os
            done = os.path.exists(r"{m / 'installed'}")
            items = [{{"line": "requests>=2", "name": "requests", "spec": ">=2", "status": "ok",
                      "installed": "2.3", "detail": "2.3"}},
                     {{"line": "psutil>=5.9", "name": "psutil", "spec": ">=5.9",
                      "status": "ok" if done else "missing",
                      "installed": "6.0" if done else None,
                      "detail": "6.0" if done else "설치 안 됨"}}]
            print(json.dumps({{"python": "x", "items": items}}, ensure_ascii=False))
        """),
        "pip": _py(tmp_path, "pip", f"""
            import sys
            open(r"{m / 'installed'}", "w").write(" ".join(sys.argv[1:]))
            print("Successfully installed psutil-6.0")
        """),
        "status": _py(tmp_path, "status", f"""
            import json, os
            ok = os.path.exists(r"{m / 'pw'}")
            print(json.dumps({{"eclass": "OS 자격증명 저장소" if ok else "없음", "github": "없음"}},
                             ensure_ascii=False))
        """),
        "password": _py(tmp_path, "password", f"""
            open(r"{m / 'pw'}", "w").write("ok")
        """),
        "doctor": _py(tmp_path, "doctor", f"""
            print("echo 줄")
            print({json.dumps(json.dumps(DOCTOR, ensure_ascii=False))})
            print("Press any key to continue . . .")
        """),
        "devices": _py(tmp_path, "devices", f"""
            print({json.dumps(json.dumps(DEVICES, ensure_ascii=False))})
        """),
    }
    F = SF.SetupFlow
    monkeypatch.setattr(F, "setup_command", lambda self: cmds["setup"])
    monkeypatch.setattr(F, "state_command", lambda self: cmds["state"])
    monkeypatch.setattr(F, "venv_exists", lambda self: (m / "venv").exists())
    monkeypatch.setattr(F, "reqcheck_command", lambda self: cmds["reqcheck"])
    monkeypatch.setattr(F, "pip_install_command", lambda self, lines: cmds["pip"] + lines)
    monkeypatch.setattr(F, "status_command", lambda self: cmds["status"])
    monkeypatch.setattr(F, "password_command", lambda self: cmds["password"])
    monkeypatch.setattr(F, "doctor_command", lambda self: cmds["doctor"])
    monkeypatch.setattr(F, "devices_command", lambda self: cmds["devices"])
    monkeypatch.setattr(eclass_fetch, "NEW_CONSOLE", 0)
    return m


def _run(app, dlg, seconds=40):
    dlg.show()
    dlg.start()
    end = time.monotonic() + seconds
    while time.monotonic() < end and dlg.close_btn.text() != "닫기":
        app.processEvents()
        time.sleep(0.02)


def test_flow_installs_verifies_fixes_asks_password_and_shows_results(app, tmp_path, fakes):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[recording.audio]\nsystem_device = "Realtek"\n', encoding="utf-8")
    dlg = SF.SetupFlow(None, cfg)
    _run(app, dlg)
    log = dlg.log.toPlainText()
    assert dlg.close_btn.text() == "닫기", log
    assert dlg.state == {"install": "ok", "verify": "ok", "password": "ok", "check": "warn"}, log
    assert (fakes / "installed").read_text(encoding="utf-8") == "psutil>=5.9"       # 빠진 줄만 설치
    assert (fakes / "pw").exists() and dlg._pw_rounds == 1          # 비밀번호 창이 열렸음
    assert "설치 완료" in log and "Press any key" in log
    assert dlg.stack.currentIndex() == 1                             # 결과 화면
    page = dlg.result_scroll.widget()
    texts = " ".join(w.text() for w in page.findChildren(gui.QLabel))
    assert "soundcard" in texts and "위의 [X] 항목을 해결하세요" in texts
    assert "스피커(Realtek)" in texts and "지금 설정과 일치" in texts
    assert "monitor = 1" in texts
    check = dlg.checklist()
    assert [c[0] for c in check] == ["X", "O", "X", "O"]             # 설정·비번·점검·장치
    assert "github.owner" in check[0][2]
    dlg.close()


def test_flow_keeps_going_when_a_step_fails(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "setup_command",
                        lambda self: [sys.executable, "-c", "import sys; sys.exit(3)"])
    monkeypatch.setattr(SF.SetupFlow, "doctor_command",
                        lambda self: [sys.executable, "-c", "print('깨진 출력')"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "fail" and ".venv" in dlg.notes["install"]
    assert dlg.state["check"] == "fail" and dlg.close_btn.text() == "닫기"
    dlg.close()


def test_password_still_missing_is_a_warning_not_a_loop(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "password_command",
                        lambda self: [sys.executable, "-c", "pass"])     # 저장 안 함
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["password"] == "warn" and dlg._pw_rounds == 1
    dlg.close()


def test_last_json_skips_bat_noise():
    assert SF.last_json('echo\n{"a": 1}\nPress any key . . .\n') == {"a": 1}
    assert SF.last_json("no json here") is None


# -- 저장 분기 ---------------------------------------------------------------


@pytest.fixture
def win_factory(app, tmp_path, monkeypatch):
    calls = {"flow": 0, "warn": []}
    monkeypatch.setattr(SF.SetupFlow, "exec",
                        lambda self: calls.__setitem__("flow", calls["flow"] + 1) or 1)
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda _p, title, text, *a, **k: calls["warn"].append(text)))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Discard)
    example = tmp_path / "config.example.toml"
    example.write_text('[general]\n# 로그 수준\nlog_level = "INFO"\n# 보관 일수\nlog_retention_days = 30\n',
                       encoding="utf-8")
    monkeypatch.setattr(gui.TimetableWindow, "_example_path", lambda self: example)
    monkeypatch.setattr(gui.TimetableWindow, "SETUP_AFTER_SAVE", True)
    made = []

    def make():
        w = gui.TimetableWindow(tmp_path / "config.toml")
        new = w.tt.copy()
        new.courses.append(M.Course("자료구조", ["mon"], "09:00", "10:00"))
        w._apply(new, "t")
        made.append(w)
        return w
    yield make, calls
    for w in made:
        w.undo.setClean()
        w.close()


def test_branch1_missing_settings_warns_opens_settings_fills_then_setup(app, tmp_path, win_factory,
                                                                        monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    (tmp_path / "config.toml").write_text('[general]\nlog_level = "DEBUG"\n', encoding="utf-8")
    seen = {}

    def accept(self):
        seen["fill"], seen["missing"] = self.fill_missing, list(self.missing)
        seen["first"] = [f.dotted for f in self._first_fields()]
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(SettingsDialog, "exec", accept)
    w = make()
    assert w.missing_settings() == ["general.log_retention_days"]
    assert w.save(show_summary=False)
    assert calls["warn"] and "1개" in calls["warn"][0]
    assert seen == {"fill": True, "missing": ["general.log_retention_days"],
                    "first": ["general.log_retention_days"]}
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'log_level = "DEBUG"' in text and "log_retention_days = 30" in text    # 예시 값으로 채움
    assert M.BEGIN in text and calls["flow"] == 1                                 # 시간표 → 설치·점검
    assert w.missing_settings() == []


def test_branch1_nothing_missing_goes_straight_to_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    (tmp_path / "config.toml").write_text(
        '[general]\nlog_level = "INFO"\nlog_retention_days = 7\n', encoding="utf-8")
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: pytest.fail("설정 창을 열면 안 됨"))
    w = make()
    assert w.save(show_summary=False)
    assert not calls["warn"] and calls["flow"] == 1


def test_branch2_no_config_settings_then_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    w = make()
    assert not w.path.exists()
    assert w.save(show_summary=False)
    assert "log_retention_days = 30" in w.path.read_text(encoding="utf-8")
    assert calls["flow"] == 1 and not calls["warn"]


def test_branch2_cancel_creates_nothing_and_skips_setup(app, tmp_path, win_factory, monkeypatch):
    from syncnote.timetable.config_wizard import SettingsDialog

    make, calls = win_factory
    monkeypatch.setattr(SettingsDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    w = make()
    assert not w.save(show_summary=False)
    assert not w.path.exists() and calls["flow"] == 0


def test_saving_while_closing_skips_setup(app, tmp_path, win_factory, monkeypatch):
    make, calls = win_factory
    (tmp_path / "config.toml").write_text(
        '[general]\nlog_level = "INFO"\nlog_retention_days = 7\n', encoding="utf-8")
    w = make()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Save)
    assert w._confirm_discard()
    assert calls["flow"] == 0 and w.undo.isClean()


def test_file_menu_can_rerun_setup(app, tmp_path, win_factory):
    make, calls = win_factory
    w = make()
    acts = {a.text(): a for m in w.menuBar().actions() for a in m.menu().actions()}
    acts["설치 · 점검 다시 실행…"].trigger()
    assert calls["flow"] == 1


# -- 1. 확인 단계 + 결과 검증 ------------------------------------------------
def test_already_installed_skips_setup_bat(app, tmp_path, fakes):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "ok" and "생략" in dlg.notes["install"]
    assert not (fakes / "setup_ran").exists()
    assert dlg.state["verify"] == "ok"
    dlg.close()


def test_partially_installed_runs_setup_and_reports_what_is_still_missing(app, tmp_path, fakes):
    (fakes / "venv").write_text("ok", encoding="utf-8")
    (fakes / "installed").write_text("ok", encoding="utf-8")
    (fakes / "no_ffmpeg").write_text("ok", encoding="utf-8")       # setup 이 못 고침
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert (fakes / "setup_ran").exists()
    assert dlg.state["install"] == "warn" and "ffmpeg" in dlg.notes["install"]
    dlg.close()


def test_nonzero_exit_but_actually_installed_counts_as_ok(app, tmp_path, fakes, monkeypatch):
    m = fakes
    monkeypatch.setattr(SF.SetupFlow, "setup_command", lambda self: [
        sys.executable, "-c",
        f"open(r'{m / 'venv'}','w').write('x'); open(r'{m / 'installed'}','w').write('x');"
        "import sys; sys.exit(1)"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "ok" and "종료 코드 1" in dlg.notes["install"]
    dlg.close()


def test_zero_exit_without_venv_counts_as_failure(app, tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(SF.SetupFlow, "setup_command",
                        lambda self: [sys.executable, "-c", "print('설치 완료')"])
    dlg = SF.SetupFlow(None, tmp_path / "config.toml")
    _run(app, dlg)
    assert dlg.state["install"] == "fail" and "만들어지지 않음" in dlg.notes["install"]
    dlg.close()
