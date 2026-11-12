"""업로드가 0건인데 종료 코드가 0 이면 안 됩니다.

check_access 의 사전 차단을 경고로 낮춘 뒤로(Actions 토큰은
contents:write 가 있어도 /repos/ 응답의 permissions.push 가 false 로
옵니다) 실패를 알아채는 자리가 여기 하나뿐입니다.

2026-11-12 실행: 파일 16개, 업로드 완료 0건, 종료 코드 0, CI 초록불.
로그에 '업로드 완료 0건' 이 그대로 찍혀 있었는데 아무도 안 봤습니다.
사전 점검을 없앴으면 사후 검증이 반드시 있어야 합니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gymhall import release as R


class _Up:
    """upload_many 가 쓰는 만큼만 흉내 냅니다."""

    slug = "owner/repo"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def upload(self, path, tag, *, prefix="", rel_path=None):
        self.calls += 1
        if self.fail:
            raise R.ReleaseError("403 권한 없음")
        return {"assets": [{"name": Path(path).name}]}


def _files(tmp_path, n=2):
    out = []
    for i in range(n):
        p = tmp_path / f"f{i}.zip"
        p.write_bytes(b"zip" + str(i).encode())
        out.append(p)
    return out


# --- tally ------------------------------------------------------------
def test_tally_counts_successes(tmp_path):
    tally = {}
    got = R.upload_many(_Up(), _files(tmp_path), "t", root=tmp_path, tally=tally)
    assert len(got) == 2 and tally["uploaded"] == 2
    assert tally["failed"] == 0


def test_tally_separates_failure_from_duplicate(tmp_path):
    """이 둘을 구분 못 하면 '전부 중복' 과 '전부 실패' 가 같아 보입니다."""
    tally = {}
    got = R.upload_many(_Up(fail=True), _files(tmp_path), "t",
                        root=tmp_path, tally=tally)
    assert got == [] and tally["failed"] == 2 and tally["skipped"] == 0


def test_tally_counts_missing_files(tmp_path):
    tally = {}
    R.upload_many(_Up(), [tmp_path / "없음.zip"], "t", root=tmp_path, tally=tally)
    assert tally["missing"] == 1 and tally["uploaded"] == 0


def test_tally_counts_name_collisions(tmp_path):
    """같은 에셋 이름 둘은 뒤엣것이 앞엣것을 지웁니다 — 조용히 넘기면 안 됩니다."""
    a = tmp_path / "a" / "같은이름.zip"
    b = tmp_path / "b" / "같은이름.zip"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    tally = {}
    # root 를 안 주면 파일 이름만으로 에셋 이름을 만들어 충돌합니다.
    R.upload_many(_Up(), [a, b], "t", tally=tally)
    assert tally["collision"] == 1, tally


def test_upload_many_still_works_without_a_tally(tmp_path):
    assert len(R.upload_many(_Up(), _files(tmp_path), "t", root=tmp_path)) == 2


# --- 종료 코드 --------------------------------------------------------
@pytest.fixture
def harness(tmp_path, monkeypatch):
    """_upload_eclass 를 돌리되 네트워크는 전부 가짜로."""
    from gymhall import cli
    from gymhall.paths import Layout
    from gymhall.state import Store

    layout = Layout(tmp_path)
    root = layout.eclass / "2026-2" / "과목가"
    root.mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"%PDF-1.4 a")

    class Cfg:
        machine_label = "공학관-401호"

        def get(self, key, default=None):
            return {"eclass.upload.exclude_menus": [],
                    "eclass.upload.allow_extensions": [],
                    "eclass.upload.max_file_mb": 0,
                    "github.tag_strategy": "monthly",
                    "github.create_missing_repos": False,
                    "github.repo_private": True}.get(key, default)

        def section(self, _k):
            return {}

    state = {"fail": False}

    class FakeUploader:
        slug = "owner/repo"

        def ensure_repo(self, **kw):
            if state.get("repo_fail"):
                raise R.ReleaseError("레포를 못 만듭니다")

        def upload(self, path, tag, *, prefix="", rel_path=None):
            if state["fail"]:
                raise R.ReleaseError("403 권한 없음")
            return {"assets": [{"name": Path(path).name}]}

    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: FakeUploader())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    def run():
        return cli._upload_eclass(Cfg(), layout,
                                  Store.open(tmp_path / "s.json"),
                                  "2026-2", dry_run=False)

    return run, state


def test_exit_code_is_one_when_nothing_uploads(harness, capsys):
    run, state = harness
    state["fail"] = True
    assert run() == 1, "전부 실패했는데 0 을 돌려줬습니다"
    out = capsys.readouterr().out
    assert "::error" in out, f"Actions 에 빨간 줄이 안 떴습니다: {out!r}"
    assert "업로드 전부 실패" in out, out


def test_exit_code_is_zero_on_success(harness):
    run, _ = harness
    assert run() == 0


def test_a_whole_repo_failing_is_also_a_failure(harness, capsys):
    """파일 단위가 아니라 레포 단위로 막히는 길이 따로 있습니다.

    ensure_repo 가 터지면 그 과목은 upload_many 까지 가지도 못합니다.
    그래서 tally 는 전부 0 이고, tally 만 보면 '올릴 게 없었다' 와
    구분이 안 됩니다.
    """
    run, state = harness
    state["repo_fail"] = True
    assert run() == 1, "레포 단위로 전부 막혔는데 0 을 돌려줬습니다"
    assert "::error" in capsys.readouterr().out


def test_a_successful_run_says_nothing_to_actions(harness, capsys):
    """성공했는데 ::error:: 가 뜨면 다음부터 아무도 안 봅니다."""
    run, _ = harness
    assert run() == 0
    assert "::error" not in capsys.readouterr().out


def test_nothing_to_upload_is_not_a_failure(tmp_path, monkeypatch):
    """폴더가 아예 없는 것은 실패가 아닙니다."""
    from gymhall import cli
    from gymhall.paths import Layout
    from gymhall.state import Store

    class Cfg:
        machine_label = ""

        def get(self, key, default=None):
            return default

        def section(self, _k):
            return {}

    assert cli._upload_eclass(Cfg(), Layout(tmp_path),
                              Store.open(tmp_path / "s.json"),
                              "2026-2", dry_run=False) == 0


def test_actions_error_is_quiet_outside_actions(monkeypatch, capsys):
    from gymhall import cli

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cli._actions_error("제목", "내용")
    assert capsys.readouterr().out == ""
