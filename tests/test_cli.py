"""CLI 인자 파싱과 명령 배선."""

from __future__ import annotations

import pytest

from syncnote import cli


def test_parser_builds():
    cli.build_parser()


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["devices"],
        ["config", "check"],
        ["secrets"],
        ["secrets", "set", "eclass"],
        ["secrets", "delete", "github"],
        ["eclass", "probe"],
        ["eclass", "probe", "--no-headless", "--courses", "3"],
        ["eclass", "sync"],
        ["eclass", "sync", "--no-upload", "--dry-run"],
        ["record", "start"],
        ["record", "start", "-l", "자료구조", "-m", "90"],
        ["record", "schedule", "--show"],
        ["record", "schedule", "--once"],
        ["record", "watch"],
        ["transcribe", "queue"],
        ["transcribe", "queue", "--show", "--limit", "2"],
        ["transcribe", "warmup"],
        ["upload", "/tmp/x"],
        ["upload", "/tmp/x", "--tag", "custom"],
        ["--log-level", "DEBUG", "doctor"],
        ["-q", "eclass", "sync"],
    ],
)
def test_every_command_parses_and_has_a_handler(argv):
    args = cli.build_parser().parse_args(argv)
    assert callable(getattr(args, "func", None)), argv


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_unknown_subcommand_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["nope"])


def test_unknown_secret_name_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["secrets", "set", "facebook"])


def test_record_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["record"])


def test_config_error_exits_cleanly(tmp_path, capsys):
    """설정이 틀렸을 때 스택트레이스가 아니라 설명이 나와야 합니다."""
    code = cli.main(["-c", str(tmp_path / "missing.toml"), "config", "check"])
    assert code != 0
    assert "없습니다" in capsys.readouterr().out


def test_toml_error_exits_cleanly(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("x = 1\nx = 2\n", encoding="utf-8")
    code = cli.main(["-c", str(bad), "config", "check"])
    assert code != 0
    assert "TOML" in capsys.readouterr().out


def test_config_check_ok(config_file, capsys):
    code = cli.main(["-c", str(config_file()), "config", "check"])
    assert code == 0
    assert "정상" in capsys.readouterr().out


def test_config_check_prints_warnings(config_file, capsys):
    path = config_file([('container = "mkv"', 'container = "mp4"')])
    cli.main(["-c", str(path), "config", "check"])
    assert "mp4" in capsys.readouterr().out


def test_timetable_show(config_file, capsys):
    path = config_file([
        ("# [[recording.timetable]]\n# name  = \"자료구조\"\n# day   = \"mon\"\n"
         "# start = \"09:00\"\n# end   = \"10:50\"",
         "[[recording.timetable]]\nname  = \"자료구조\"\nday   = \"mon\"\n"
         "start = \"09:00\"\nend   = \"10:50\""),
    ])
    code = cli.main(["-c", str(path), "-q", "record", "schedule", "--show"])
    assert code == 0
    out = capsys.readouterr().out
    assert "자료구조" in out
    assert "다음 녹화" in out


def test_timetable_show_empty_returns_error(config_file, capsys):
    code = cli.main(["-c", str(config_file()), "-q", "record", "schedule", "--show"])
    assert code == 1
    assert "비어 있습니다" in capsys.readouterr().out


def test_transcribe_queue_show_empty(config_file, capsys):
    code = cli.main(["-c", str(config_file()), "-q", "transcribe", "queue", "--show"])
    assert code == 0
    assert "비어 있습니다" in capsys.readouterr().out


def test_upload_missing_path(config_file, capsys):
    code = cli.main(["-c", str(config_file()), "-q", "upload", "/nonexistent/x"])
    assert code == 1
    assert "없습니다" in capsys.readouterr().out


# --- --dry-run 의 뜻 ---------------------------------------------------
# 2026-11-13 Windows 실기에서 `record start --dry-run` 이 1분을 통째로
# 녹화했습니다. 도움말이 비어 있어서 "아무것도 안 하는 옵션" 으로
# 읽힌 것입니다. 실제로는 **녹화와 전사는 하고 업로드만 흉내** 냅니다.
def test_every_dry_run_flag_explains_itself():
    parser = cli.build_parser()
    empty = []

    def walk(p, path=""):
        for action in p._actions:
            if ("--dry-run" in getattr(action, "option_strings", [])
                    and not (action.help or "").strip()):
                empty.append(path or "(최상위)")
            sub = getattr(action, "choices", None)
            if isinstance(sub, dict):
                for name, child in sub.items():
                    walk(child, f"{path} {name}".strip())

    walk(parser)
    assert not empty, f"--dry-run 설명이 비어 있습니다: {empty}"


def test_record_start_dry_run_says_recording_still_happens():
    """'업로드만 흉내' 라고만 적으면 녹화도 안 하는 줄 압니다."""
    parser = cli.build_parser()
    args = parser.parse_args(["record", "start", "--dry-run"])
    assert args.dry_run

    rec = parser._actions[-1].choices["record"]
    start = rec._actions[-1].choices["start"]
    help_text = next(a.help for a in start._actions
                     if "--dry-run" in a.option_strings)
    assert "녹화" in help_text, help_text


def test_timetable_edit_without_pyside_explains_which_python(monkeypatch, capsys, tmp_path):
    """PySide6 가 없으면 '어느 파이썬에' 깔지까지 알려 주고 1 로 끝납니다."""
    import sys

    import syncnote.timetable as tt_pkg
    from syncnote import cli

    # import 실패 흉내. sys.modules 만 막으면 안 됩니다 — pytest 가 테스트를
    # 모을 때 test_timetable_gui 가 gui 를 이미 import 해 패키지 속성으로
    # 붙어 있고, `from .timetable import gui` 는 그 속성을 먼저 씁니다.
    # 그러면 진짜 편집기 창이 떠서 테스트가 영원히 멈춥니다(실제로 그랬음).
    monkeypatch.setitem(sys.modules, "syncnote.timetable.gui", None)
    monkeypatch.delattr(tt_pkg, "gui", raising=False)
    # 그래도 혹시 GUI 가 뜨면 멈추지 말고 실패하게.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    rc = cli.main(["-c", str(tmp_path / "config.toml"), "timetable", "edit"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "PySide6" in out and sys.executable in out and "-m pip install PySide6" in out
