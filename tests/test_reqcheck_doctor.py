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
