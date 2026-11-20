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

from syncnote import release as R


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
    from syncnote import cli
    from syncnote.paths import Layout
    from syncnote.state import Store

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
    from syncnote import cli
    from syncnote.paths import Layout
    from syncnote.state import Store

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
    from syncnote import cli

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    cli._actions_error("제목", "내용")
    assert capsys.readouterr().out == ""


# --- U3: 이름 충돌도 실패입니다 ----------------------------------------
# 2026-11-12 에 16개 중 14개가 사라진 원인이 에셋 이름 충돌이었습니다.
#
# 주의: _upload_eclass 안에서는 **자연적으로 충돌이 안 납니다.** 에셋
# 이름이 최상위 폴더 이름(= zip 이름)에서 나오는데, 같은 실행에서 두
# 폴더가 같은 이름일 수 없기 때문입니다. 그래서 blocked 합계의
# collision 항은 지금 도달 불가 방어입니다.
#
# 도달 불가라고 빼면 안 됩니다 — upload_many 는 cmd_upload(임의
# 폴더 업로드)도 씁니다. 거기서는 서로 다른 하위 폴더의 같은 이름
# 파일이 실제로 겹칩니다. 그리고 zip 묶는 방식이 바뀌면 여기서도
# 납니다. 그때 조용히 0 으로 끝나면 안 됩니다.
#
# 그래서 '자연 발생' 대신 tally 를 직접 채워 **판정**을 검사합니다.
def test_a_name_collision_alone_makes_the_run_fail(harness, capsys, monkeypatch):
    from syncnote import release as real_release

    run, _ = harness

    def _collide(uploader, files, tag, **kw):
        tally = kw.get("tally")
        if tally is not None:
            tally["collision"] = tally.get("collision", 0) + 1
        return []

    monkeypatch.setattr(real_release, "upload_many", _collide)
    assert run() == 1, "이름이 겹쳐 하나도 안 올라갔는데 성공으로 끝났습니다"
    out = capsys.readouterr().out
    assert "::error" in out and "이름충돌 1" in out, out


def test_collisions_are_not_confused_with_duplicates(harness, capsys, monkeypatch):
    """'이미 올라가 있어서 건너뜀' 은 실패가 아닙니다."""
    from syncnote import release as real_release

    run, _ = harness

    def _all_dupes(uploader, files, tag, **kw):
        tally = kw.get("tally")
        if tally is not None:
            tally["skipped"] = tally.get("skipped", 0) + len(files)
        return []

    monkeypatch.setattr(real_release, "upload_many", _all_dupes)
    assert run() == 0, "전부 중복인데 실패로 끝났습니다"
    assert "::error" not in capsys.readouterr().out


# --- cmd_upload 도 같은 자리입니다 -------------------------------------
# 변이 세트가 eclass 경로만 봐서 이쪽은 안 걸렸습니다. 전부 실패해도
# "완료 0건" 을 찍고 종료 코드 0 이었습니다.
@pytest.fixture
def upload_cmd(tmp_path, monkeypatch):
    from syncnote import cli
    from syncnote.paths import Layout

    src = tmp_path / "자료"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"%PDF a")
    (src / "b.pdf").write_bytes(b"%PDF b")

    class Cfg:
        machine_label = "pc"
        data_dir = tmp_path

        def get(self, key, default=None):
            return default

        def section(self, _k):
            return {}

    state = {"fail": False}

    class FakeUploader:
        slug = "owner/repo"

        def upload(self, path, tag, *, prefix="", rel_path=None):
            if state["fail"]:
                raise R.ReleaseError("403")
            return {"assets": [{"name": Path(path).name}]}

    monkeypatch.setattr(cli, "_load", lambda _a: (Cfg(), Layout(tmp_path)))
    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: FakeUploader())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    class _Args:
        path = str(src)
        tag = "t"
        dry_run = False
        course = ""

    return (lambda: cli.cmd_upload(_Args())), state


def test_manual_upload_fails_loudly_when_nothing_uploads(upload_cmd, capsys):
    run, state = upload_cmd
    state["fail"] = True
    assert run() == 1, "전부 실패했는데 0 을 돌려줬습니다"
    assert "::error" in capsys.readouterr().out


def test_manual_upload_returns_zero_on_success(upload_cmd, capsys):
    run, _ = upload_cmd
    assert run() == 0
    assert "::error" not in capsys.readouterr().out


# --- 릴리스 이름 -------------------------------------------------------
def test_eclass_release_is_named_by_date(tmp_path, monkeypatch):
    """e-class 자료는 eclass-년-월-일 릴리스로 올라가야 합니다.

    녹화용 tag_strategy(monthly)를 같이 쓰면 eclass-2026-11 이 됩니다.
    부품(tag_for)이 아니라 _upload_eclass 가 실제로 넘기는 태그를 봅니다.
    """
    from datetime import date

    from syncnote import cli
    from syncnote.paths import Layout
    from syncnote.state import Store

    layout = Layout(tmp_path)
    root = layout.eclass / "2026-2" / "과목가"
    root.mkdir(parents=True)
    (root / "a.pdf").write_bytes(b"%PDF-1.4 a")

    class Cfg:
        machine_label = "pc"

        def get(self, key, default=None):
            # 녹화 쪽은 monthly 로 둡니다. 이게 새어 들어오면 안 됩니다.
            return {"github.tag_strategy": "monthly"}.get(key, default)

        def section(self, _k):
            return {}

    tags = []

    class FakeUploader:
        slug = "owner/repo"

        def ensure_repo(self, **kw):
            pass

        def upload(self, path, tag, *, prefix="", rel_path=None):
            tags.append(tag)
            return {"assets": [{"name": Path(path).name}]}

    monkeypatch.setattr(cli, "_uploader", lambda *a, **k: FakeUploader())
    cli._upload_eclass(Cfg(), layout, Store.open(tmp_path / "s.json"),
                       "2026-2", dry_run=False)

    assert tags, "업로드가 한 번도 불리지 않았습니다"
    expected = f"eclass-{date.today():%Y-%m-%d}"
    assert set(tags) == {expected}, tags
