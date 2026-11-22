"""[recording.triggers] 스위치가 실제로 녹화를 켜고 끄는지 + 기본값.

예전에는 manual·schedule_enabled·watch_enabled 를 어디서도 읽지 않아 false 로 적어도
record start/schedule/watch 가 그대로 녹화했습니다. 이제 진짜 스위치이고, 예전 동작을
유지하도록 기본은 true 입니다.
"""

import types

import pytest

from syncnote import cli
from syncnote import config as C
from syncnote.paths import Layout

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Cfg:
    machine_label = "PC"

    def __init__(self, d):
        self._d = d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def section(self, _k):
        return {}


class Reached(Exception):
    """스위치를 지나 녹화 준비까지 갔다는 표시."""


def _load(monkeypatch, tmp_path, d):
    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(d), Layout(tmp_path)))


def test_manual_off_refuses_record_start(monkeypatch, tmp_path, capsys):
    _load(monkeypatch, tmp_path, {"recording.triggers.manual": False})
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **k: pytest.fail("녹화로 가면 안 됨"))
    args = types.SimpleNamespace(label="수업", course="", minutes=1, no_upload=True, dry_run=True)
    assert cli.cmd_record_start(args) == 1
    assert "manual = false" in capsys.readouterr().out


def test_schedule_off_stops_but_show_still_works(monkeypatch, tmp_path, capsys):
    from syncnote.recorder import schedule as S

    _load(monkeypatch, tmp_path, {"recording.triggers.schedule": False})
    monkeypatch.setattr(S, "parse", lambda *_a: pytest.fail("시간표를 읽으면 안 됨"))
    run = types.SimpleNamespace(show=False, once=True, no_upload=True, dry_run=True)
    assert cli.cmd_record_schedule(run) == 0
    assert "schedule = false" in capsys.readouterr().out

    def parsed(*_a):
        raise Reached
    monkeypatch.setattr(S, "parse", parsed)
    with pytest.raises(Reached):                                     # --show 는 꺼져 있어도 됨
        cli.cmd_record_schedule(types.SimpleNamespace(show=True, once=True, no_upload=True,
                                                      dry_run=True))


def test_watch_off_stops_before_watching(monkeypatch, tmp_path):
    from syncnote.recorder import watcher as W

    _load(monkeypatch, tmp_path, {"recording.triggers.watch": False})
    monkeypatch.setattr(W, "Watcher", lambda **_k: pytest.fail("감시를 시작하면 안 됨"))
    args = types.SimpleNamespace(label=None, course="", language=None, once=True,
                                 no_upload=True, dry_run=True)
    assert cli.cmd_record_watch(args) == 0


@pytest.mark.parametrize("value", [True, None])                       # true 또는 키 없음
def test_switch_on_or_missing_goes_ahead(monkeypatch, tmp_path, value):
    from syncnote.recorder import watcher as W

    d = {} if value is None else {"recording.triggers.watch": value}
    _load(monkeypatch, tmp_path, d)

    def built(**_k):
        raise Reached
    monkeypatch.setattr(W, "Watcher", built)
    with pytest.raises(Reached):
        cli.cmd_record_watch(types.SimpleNamespace(label=None, course="", language=None,
                                                   once=True, no_upload=True, dry_run=True))


def test_defaults_keep_old_behaviour_and_stream_overrides_existing():
    trig = C.DEFAULTS["recording"]["triggers"]
    assert (trig["manual"], trig["schedule"], trig["watch"]) == (True, True, True)
    assert "schedule_enabled" not in trig and "watch_enabled" not in trig
    ts = C.DEFAULTS["stream"]["tailscale"]
    assert (ts["require_online"], ts["override_existing"], ts["teardown"]) == (True, True, True)
    ex = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    assert ex["recording"]["triggers"]["schedule"] is True
    assert ex["recording"]["triggers"]["watch"] is True
    assert ex["stream"]["tailscale"]["override_existing"] is True

    from syncnote.stream import live
    assert live.StreamSettings().ts_override_existing is True


def test_old_false_keys_in_existing_config_do_not_stop_auto_recording(monkeypatch, tmp_path):
    """기존 config.toml(옛 예시를 복사)에는 schedule_enabled·watch_enabled = false 가 있습니다.
    예전엔 무시됐으니, 업데이트해도 자동 녹화가 멈추면 안 됩니다."""
    from syncnote.recorder import watcher as W

    _load(monkeypatch, tmp_path, {"recording.triggers.schedule_enabled": False,
                                  "recording.triggers.watch_enabled": False})

    def built(**_k):
        raise Reached
    monkeypatch.setattr(W, "Watcher", built)
    with pytest.raises(Reached):
        cli.cmd_record_watch(types.SimpleNamespace(label=None, course="", language=None,
                                                   once=True, no_upload=True, dry_run=True))


def test_old_keys_get_a_note_to_delete(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n'
                 '[recording.triggers]\nschedule_enabled = false\nwatch_enabled = false\n',
                 encoding="utf-8")
    notes = " ".join(C.warnings_for(C.load(p)))
    assert "schedule_enabled" in notes and "지워도 됩니다" in notes
