"""Qt 없이 도는 부분: reqcheck, doctor/devices --json (CI 에서도 돎)."""

import json
import textwrap

from syncnote import cli, reqcheck

# -- reqcheck (Qt 없이) ------------------------------------------------------


def test_reqcheck_statuses():
    text = textwrap.dedent("""
        # 주석
        pytest>=1.0          # 설치돼 있음
        pytest<1             # 버전 안 맞음
        zz-not-installed-pkg>=1
        tomli>=2.0 ; python_version < "2.0"
        --index-url https://example.invalid
    """)
    got = {(i["line"], i["status"]) for i in reqcheck.check(text)}
    assert ("pytest>=1.0", "ok") in got and ("pytest<1", "mismatch") in got
    assert ("zz-not-installed-pkg>=1", "missing") in got
    assert any(s == "skip" for ln, s in got if ln.startswith("tomli"))
    assert len(got) == 4                                   # 옵션 줄은 뺌


def test_reqcheck_reads_the_real_requirements_file():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    names = {i["name"].lower() for i in reqcheck.check(text)}
    assert {"requests", "keyring", "pyside6"} <= names


# -- doctor / devices --json --------------------------------------------------


def test_doctor_json_is_structured_and_text_unchanged(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[general]\ndata_dir = "' + (tmp_path / "d").as_posix() + '"\n'
                   '[github]\nowner = "o"\nrepo = "r"\n[eclass]\nenabled = false\n'
                   '[recording]\nenabled = false\n[transcribe]\nenabled = false\n',
                   encoding="utf-8")
    cli.main(["-q", "-c", str(cfg), "doctor", "--json"])
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["info"][0][0] == "Python" and any(s["title"] == "자격증명" for s in rep["sections"])
    assert rep["verdict"] and isinstance(rep["ok"], bool)
    cli.main(["-q", "-c", str(cfg), "doctor"])
    text = capsys.readouterr().out
    assert "환경 점검" in text and "파이썬 패키지" in text and not text.lstrip().startswith("{")


def test_doctor_json_reports_config_errors(tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[eclass]\nenabled = true\nusername = ""\n', encoding="utf-8")
    cli.main(["-q", "-c", str(cfg), "doctor", "--json"])
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rep["ok"] is False and "eclass.username" in rep["config_error"]


def test_devices_json_shape(capsys):
    cli.main(["devices", "--json"])
    d = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [g["kind"] for g in d["groups"]] == ["loopback", "microphone", "speaker"]
    assert d["groups"][0]["config_key"] == "recording.audio.system_device"
    assert "monitors" in d


# -- setupcheck ---------------------------------------------------------------
def test_setupcheck_missing_reasons():
    from syncnote import setupcheck

    full = {"in_venv": True, "reqs_bad": [], "ffmpeg": "f.exe", "chromium": True}
    assert setupcheck.missing(full) == []
    gaps = setupcheck.missing({"in_venv": False, "reqs_bad": ["psutil"], "ffmpeg": None,
                               "chromium": False})
    assert gaps == ["가상환경(.venv) 없음", "패키지: psutil", "ffmpeg 없음",
                    "Playwright Chromium 없음"]


def test_setupcheck_finds_chromium_where_playwright_puts_it(tmp_path, monkeypatch):
    from syncnote import setupcheck

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert not setupcheck.chromium_installed()
    (tmp_path / "chromium-1187").mkdir()
    assert setupcheck.chromium_installed()


def test_setupcheck_report_shape(tmp_path):
    from syncnote import setupcheck

    req = tmp_path / "r.txt"
    req.write_text("pytest>=1\nzz-not-installed-pkg\n", encoding="utf-8")
    rep = setupcheck.report(req)
    assert rep["reqs_bad"] == ["zz-not-installed-pkg"] and isinstance(rep["in_venv"], bool)


# -- reqcheck --import (B2) · deferred_install (A1) ----------------------------
def test_reqcheck_import_failure_is_broken(monkeypatch):
    monkeypatch.setattr(reqcheck, "try_import",
                        lambda m: "OSError: DLL load failed" if m == "pytest" else None)
    item = reqcheck.check_line("pytest>=1", imports=True)
    assert item["status"] == "broken" and "DLL load failed" in item["detail"]
    assert reqcheck.check_line("pytest>=1")["status"] == "ok"       # --import 없으면 버전만


def test_import_names_ignores_path_fragments(monkeypatch):
    monkeypatch.setattr(reqcheck.metadata, "packages_distributions",
                        lambda: {"PySide6/QtCore": ["PySide6"], "PySide6": ["PySide6"],
                                 "_priv": ["PySide6"], "faster_whisper": ["faster-whisper"]})
    assert reqcheck.import_names("PySide6") == ["PySide6"]
    assert reqcheck.import_names("faster-whisper") == ["faster_whisper"]
    assert reqcheck.import_names("unknown-pkg") == ["unknown_pkg"]


def test_deferred_install_parses_and_installs_then_relaunches(monkeypatch):
    from syncnote import deferred_install as D

    assert D.parse(["--pid", "42", "--force", "--relaunch", "--", "a>=1", "b"]) == (
        42, True, True, ["a>=1", "b"])
    assert D.pip_args(["a"], force=True)[-3:] == ["--force-reinstall", "--no-deps", "a"]
    calls = {}
    monkeypatch.setattr(D, "wait_pid", lambda pid, timeout=120.0: calls.setdefault("pid", pid) or True)
    monkeypatch.setattr(D.subprocess, "call", lambda args: calls.setdefault("pip", args) and 0)
    monkeypatch.setattr(D.subprocess, "Popen", lambda args, **k: calls.setdefault("relaunch", args))
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    assert D.main(["--pid", "7", "--relaunch", "--", "psutil>=5.9"]) == 0
    assert calls["pid"] == 7 and calls["pip"][-1] == "psutil>=5.9"
    assert "timetable" in " ".join(calls["relaunch"]) or "timetable_edit.bat" in " ".join(calls["relaunch"])


def test_deferred_install_waits_for_a_real_process():
    import subprocess as sp
    import sys as _sys

    from syncnote import deferred_install as D

    p = sp.Popen([_sys.executable, "-c", "import time; time.sleep(0.5)"])
    assert D.wait_pid(p.pid, timeout=10)
    assert D.wait_pid(0)
