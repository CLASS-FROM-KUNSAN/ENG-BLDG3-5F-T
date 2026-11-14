"""backup home / backup prune 의 연결부: 종료 코드와 넘기는 인자.

부품(run_backup, run_prune)은 각자 시험합니다. 여기서는 cli 가 그것을
**올바르게 부르고, 결과를 올바른 종료 코드로 바꾸는지**만 봅니다.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from gymhall import cli
from gymhall.backup.homestate import HomeState
from gymhall.paths import Layout


class _Cfg:
    machine_label = "pc"

    def __init__(self, home, **over):
        self.data_dir = str(home.parent / "data")
        self.values = {
            "backup.home.enabled": True,
            "backup.home.root": str(home),
            "backup.home.prune.enabled": True,
            "github.owner": "o",
            "github.repo": "r",
            **over,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    layout = Layout(tmp_path / "data")
    layout.ensure()
    box = {"cfg": _Cfg(home), "calls": []}
    monkeypatch.setattr(cli, "_load", lambda _a: (box["cfg"], layout))
    # 홈 밖 경로는 거부하므로, 테스트의 홈을 사용자 홈으로 둡니다.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    return box, home, layout


def _args(**kw):
    return argparse.Namespace(config=None, dry_run=False, **kw)


def _backup_result(**kw):
    base = dict(seq=0, uploaded_files=1, skipped_unchanged=0, failed_bundles=0,
                unreadable=0, excluded=0, bytes=1, manifest_failed=False, links=1)
    base.update(kw)
    return SimpleNamespace(**base)


def _prune_result(**kw):
    base = dict(chain_ok=True, deleted=1, errors=0, missing_remote=0)
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_backup(monkeypatch, box, result):
    import gymhall.backup.run as run_mod

    def fake(**kw):
        box["calls"].append(kw)
        assert isinstance(kw["state"], HomeState)
        return result

    monkeypatch.setattr(run_mod, "run_backup", fake)


def _patch_prune(monkeypatch, box, result):
    import gymhall.backup.prune as prune_mod

    def fake(**kw):
        box["calls"].append(kw)
        assert isinstance(kw["state"], HomeState)
        return result

    monkeypatch.setattr(prune_mod, "run_prune", fake)


# --- backup home --------------------------------------------------------
def test_backup_disabled_does_nothing(env, monkeypatch):
    box, home, _ = env
    box["cfg"] = _Cfg(home, **{"backup.home.enabled": False})
    _patch_backup(monkeypatch, box, _backup_result())
    assert cli.cmd_backup_home(_args()) == 0
    assert box["calls"] == []


def test_backup_success_is_zero_and_uses_home_tag(env, monkeypatch):
    box, home, layout = env
    _patch_backup(monkeypatch, box, _backup_result())
    assert cli.cmd_backup_home(_args()) == 0
    call = box["calls"][0]
    assert call["tag"].startswith("home-") and len(call["tag"]) == len("home-2026-11-13")
    assert call["home"] == home
    assert (layout.state / "home_backup.sqlite").exists()


@pytest.mark.parametrize("result", [
    _backup_result(failed_bundles=2),
    _backup_result(manifest_failed=True),
    # 앞 고리는 성공(seq=0)했지만 뒤 고리의 매니페스트가 실패
    _backup_result(seq=0, manifest_failed=True),
])
def test_backup_failures_exit_one(env, monkeypatch, capsys, result):
    box, *_ = env
    _patch_backup(monkeypatch, box, result)
    assert cli.cmd_backup_home(_args()) == 1
    assert "::error" in capsys.readouterr().out


def test_backup_ignores_course_repos(env, monkeypatch):
    """course_repos 가 있어도 홈 백업은 기본 레포로 갑니다(과목과 무관)."""
    box, home, _ = env
    box["cfg"] = _Cfg(home, **{"eclass.course_repos": [{"name": "x", "repo": "a/b"}]})
    _patch_backup(monkeypatch, box, _backup_result())
    assert cli.cmd_backup_home(_args()) == 0
    assert box["calls"][0]["uploader"].slug == "o/r"


# --- backup prune -------------------------------------------------------
def test_prune_disabled_does_nothing_unless_dry_run(env, monkeypatch):
    box, home, _ = env
    box["cfg"] = _Cfg(home, **{"backup.home.prune.enabled": False})
    _patch_prune(monkeypatch, box, _prune_result())
    assert cli.cmd_backup_prune(_args()) == 0
    assert box["calls"] == []
    assert cli.cmd_backup_prune(argparse.Namespace(config=None, dry_run=True)) == 0
    assert box["calls"] and box["calls"][0]["dry_run"] is True


def test_prune_protects_program_data_and_config(env, monkeypatch):
    box, _, layout = env
    _patch_prune(monkeypatch, box, _prune_result())
    assert cli.cmd_backup_prune(_args()) == 0
    protect = [Path(p).resolve() for p in box["calls"][0]["protect"]]
    repo_root = Path(cli.__file__).resolve().parents[2]
    assert repo_root in protect
    assert Path(box["cfg"].data_dir).resolve() in protect
    assert any(p.name == "config.toml" for p in protect)


def test_prune_reads_chain_even_in_dry_run(env, monkeypatch):
    """dry-run 업로더는 네트워크를 안 씁니다. 그걸 넘기면 사슬 확인이 늘 실패합니다."""
    box, *_ = env
    _patch_prune(monkeypatch, box, _prune_result())
    cli.cmd_backup_prune(argparse.Namespace(config=None, dry_run=True))
    assert box["calls"][0]["uploader"].dry_run is False


@pytest.mark.parametrize("result", [
    _prune_result(chain_ok=False, deleted=0),
    _prune_result(errors=3),
])
def test_prune_failures_exit_one(env, monkeypatch, capsys, result):
    box, *_ = env
    _patch_prune(monkeypatch, box, result)
    assert cli.cmd_backup_prune(_args()) == 1


def test_parser_has_backup_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["backup", "home", "--dry-run"]).func is cli.cmd_backup_home
    assert parser.parse_args(["backup", "prune"]).func is cli.cmd_backup_prune


# --- 리뷰 후 추가: 잠금, 홈 밖 경로, 설치 ID, 보호 경로 ---------------------
def test_machine_and_prefix_carry_install_id(env, monkeypatch):
    """설정을 복사한 PC 끼리 사슬/에셋 이름이 겹치면 서로의 묶음을 덮어씁니다."""
    box, _, layout = env
    _patch_backup(monkeypatch, box, _backup_result())
    cli.cmd_backup_home(_args())
    with HomeState.open(layout.state / "home_backup.sqlite") as st:
        iid = st.install_id()
    call = box["calls"][0]
    assert call["machine"] == f"pc#{iid}"
    assert call["prefix"].startswith("pc_") and iid in call["prefix"]
    # 정리도 같은 machine 으로 사슬을 봅니다.
    _patch_prune(monkeypatch, box, _prune_result())
    cli.cmd_backup_prune(_args())
    assert box["calls"][1]["machine"] == f"pc#{iid}"


def test_install_id_is_stable(tmp_path):
    db = tmp_path / "s.sqlite"
    with HomeState.open(db) as st:
        a = st.install_id()
    with HomeState.open(db) as st:
        assert st.install_id() == a


def test_root_outside_user_home_is_refused(env, monkeypatch, tmp_path):
    box, home, _ = env
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    box["cfg"] = _Cfg(home, **{"backup.home.root": str(outside)})
    _patch_backup(monkeypatch, box, _backup_result())
    _patch_prune(monkeypatch, box, _prune_result())
    assert cli.cmd_backup_home(_args()) == 1
    assert cli.cmd_backup_prune(_args()) == 1
    assert box["calls"] == []


def test_root_parent_of_home_is_refused(env, monkeypatch):
    box, home, _ = env
    box["cfg"] = _Cfg(home, **{"backup.home.root": str(home.parent)})
    _patch_prune(monkeypatch, box, _prune_result())
    assert cli.cmd_backup_prune(_args()) == 1
    assert box["calls"] == []


def test_prune_skips_while_backup_holds_the_lock(env, monkeypatch):
    from gymhall.backup.lock import exclusive

    box, _, layout = env
    _patch_prune(monkeypatch, box, _prune_result())
    with exclusive(layout.state / "home_backup.lock") as got:
        assert got
        assert cli.cmd_backup_prune(_args()) == 0
    assert box["calls"] == []


def test_backup_gives_up_when_lock_stays_busy(env, monkeypatch, capsys):
    from gymhall.backup.lock import exclusive

    box, _, layout = env
    monkeypatch.setattr(cli, "HOME_LOCK_WAIT_SEC", 0)
    _patch_backup(monkeypatch, box, _backup_result())
    with exclusive(layout.state / "home_backup.lock") as got:
        assert got
        assert cli.cmd_backup_home(_args()) == 1
    assert box["calls"] == []
    # 풀리면 다시 됩니다.
    assert cli.cmd_backup_home(_args()) == 0


def test_prune_protects_the_running_python(env, monkeypatch):
    import sys

    box, *_ = env
    _patch_prune(monkeypatch, box, _prune_result())
    cli.cmd_backup_prune(_args())
    protect = {Path(p).resolve() for p in box["calls"][0]["protect"]}
    assert Path(sys.prefix).resolve() in protect
    assert Path(sys.executable).resolve().parent in protect


def test_prune_missing_remote_exits_one(env, monkeypatch, capsys):
    box, *_ = env
    _patch_prune(monkeypatch, box, _prune_result(missing_remote=2))
    assert cli.cmd_backup_prune(_args()) == 1
    assert "::error" in capsys.readouterr().out


def test_backup_not_ok_exits_one(env, monkeypatch):
    box, *_ = env
    _patch_backup(monkeypatch, box, _backup_result(ok=False))
    assert cli.cmd_backup_home(_args()) == 1
