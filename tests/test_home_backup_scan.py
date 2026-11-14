"""홈 스캔: 무엇을 넣고 무엇을 빼는가.

빼야 할 것을 넣으면 비밀이 GitHub 로 가고, 넣어야 할 것을 빼면 정리가
그 파일을 지울 근거가 없어 남을 뿐이지만 백업도 없습니다. 둘 다 봅니다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gymhall.backup import scan as scan_mod
from gymhall.backup.scan import (
    DEFAULT_EXCLUDE_NAMES,
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    file_attributes,
    is_cloud_placeholder,
    is_reparse_point,
    is_sensitive,
    iter_home,
    scan_home,
)


def _w(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _rels(home: Path, res) -> set[str]:
    return {p.relative_to(home).as_posix() for p, _, _ in res.files}


def test_top_level_appdata_and_dot_dirs_are_excluded_but_nested_are_kept(tmp_path):
    home = tmp_path / "home"
    _w(home / "AppData/Local/big.db")
    _w(home / ".vscode/settings.json")
    _w(home / ".cache/x.bin")
    _w(home / "proj/.git/HEAD")
    _w(home / "proj/node_modules/a/index.js")
    _w(home / "proj/.venv/pyvenv.cfg")
    _w(home / "proj/AppData/keep.txt")          # 최상위가 아니면 넣습니다
    _w(home / ".bashrc")                          # 최상위 '파일' 은 디렉터리가 아닙니다
    _w(home / "Documents/과제/보고서.hwp")
    res = scan_home(home, exclude_names=DEFAULT_EXCLUDE_NAMES)
    got = _rels(home, res)
    assert got == {
        "proj/.git/HEAD", "proj/node_modules/a/index.js", "proj/.venv/pyvenv.cfg",
        "proj/AppData/keep.txt", ".bashrc", "Documents/과제/보고서.hwp",
    }
    reasons = {p.name: r for p, r in res.excluded}
    assert set(reasons) >= {"AppData", ".vscode", ".cache"}


def test_appdata_match_ignores_case(tmp_path):
    home = tmp_path / "h"
    _w(home / "appdata/x.txt")
    assert _rels(home, scan_home(home, exclude_names=[])) == set()


@pytest.mark.parametrize("name", [
    "NTUSER.DAT", "ntuser.dat.LOG1", "UsrClass.dat", "UsrClass.dat.LOG2", "PaceKeyChain",
    ".env", ".env.local", "server.pem", "x.KEY", "cert.pfx", "c.p12", "id_rsa", "id_rsa.pub",
    "id_ecdsa", "id_ed25519.pub", "id_dsa", "pw.kdbx", "config.toml",
])
def test_sensitive_names_are_excluded(tmp_path, name):
    home = tmp_path / "h"
    _w(home / "Documents" / name)
    _w(home / "Documents" / "ok.txt")
    res = scan_home(home, exclude_names=DEFAULT_EXCLUDE_NAMES)
    assert _rels(home, res) == {"Documents/ok.txt"}
    assert any(p.name == name and "민감" in r for p, r in res.excluded)


def test_sensitive_names_at_home_top_level_too(tmp_path):
    home = tmp_path / "h"
    _w(home / "NTUSER.DAT")
    _w(home / "ntuser.ini")
    _w(home / "a.txt")
    assert _rels(home, scan_home(home, exclude_names=DEFAULT_EXCLUDE_NAMES)) == {"a.txt"}


def test_exclude_names_can_be_replaced(tmp_path):
    home = tmp_path / "h"
    _w(home / "config.toml")
    _w(home / "notes.secret")
    assert _rels(home, scan_home(home, exclude_names=["*.secret"])) == {"config.toml"}


def test_default_list_is_used_when_none(tmp_path):
    home = tmp_path / "h"
    _w(home / "id_rsa")
    assert _rels(home, scan_home(home, exclude_names=None)) == set()


def test_is_sensitive_is_case_insensitive():
    assert is_sensitive("ID_RSA", ["id_rsa*"])
    assert not is_sensitive("rsa_id", ["id_rsa*"])
    assert not is_sensitive("x", [""])


def test_files_carry_size_and_mtime(tmp_path):
    home = tmp_path / "h"
    f = _w(home / "a.bin", b"12345")
    os.utime(f, ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))
    (path, size, mtime) = scan_home(home, exclude_names=[]).files[0]
    assert path == f and size == 5 and mtime == f.stat().st_mtime_ns


def test_files_are_sorted_by_relative_path(tmp_path):
    home = tmp_path / "h"
    for rel in ("b/z.txt", "a/y.txt", "a.txt", "b/a.txt"):
        _w(home / rel)
    rels = [p.relative_to(home).as_posix() for p, _, _ in scan_home(home, exclude_names=[]).files]
    assert rels == sorted(rels)


def test_skip_dirs_excludes_the_gymhall_data_folder(tmp_path):
    home = tmp_path / "h"
    _w(home / "gymhall-data/recordings/a.mkv")
    _w(home / "Documents/a.txt")
    res = scan_home(home, exclude_names=[], skip_dirs=[home / "gymhall-data"])
    assert _rels(home, res) == {"Documents/a.txt"}


# ---------------------------------------------------------------------------
# 속성 검사 — 어느 OS 에서든 가짜 stat 으로 확인합니다
# ---------------------------------------------------------------------------
def test_attributes_missing_off_windows_are_zero():
    st = SimpleNamespace(st_size=1)
    assert file_attributes(st) == 0
    assert not is_reparse_point(st) and not is_cloud_placeholder(st)


def test_reparse_attribute_is_detected():
    assert is_reparse_point(SimpleNamespace(st_file_attributes=FILE_ATTRIBUTE_REPARSE_POINT | 0x10))
    assert not is_reparse_point(SimpleNamespace(st_file_attributes=0x10 | 0x20))


@pytest.mark.parametrize("attr", [FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_OPEN,
                                  FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS])
def test_cloud_placeholder_attributes_are_detected(attr):
    assert is_cloud_placeholder(SimpleNamespace(st_file_attributes=attr | 0x20))


def test_plain_file_attributes_are_not_cloud():
    assert not is_cloud_placeholder(SimpleNamespace(st_file_attributes=0x20 | 0x80))


class _FakeStat:
    def __init__(self, real, attrs):
        self._real = real
        self.st_file_attributes = attrs

    def __getattr__(self, name):
        return getattr(self._real, name)


def _patch_attrs(monkeypatch, attrs_by_name: dict[str, int]):
    """scandir 가 돌려주는 항목의 속성을 이름으로 바꿔 끼웁니다."""
    real_scandir = os.scandir

    class _Entry:
        def __init__(self, e):
            self._e = e
            self.name = e.name
            self.path = e.path

        def stat(self, follow_symlinks=True):
            st = self._e.stat(follow_symlinks=follow_symlinks)
            if self.name in attrs_by_name:
                return _FakeStat(st, attrs_by_name[self.name])
            return st

        def is_symlink(self):
            return self._e.is_symlink()

        def is_dir(self, follow_symlinks=True):
            return self._e.is_dir(follow_symlinks=follow_symlinks)

        def is_file(self, follow_symlinks=True):
            return self._e.is_file(follow_symlinks=follow_symlinks)

    class _It:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            return (_Entry(e) for e in self._it)

    monkeypatch.setattr(scan_mod.os, "scandir", _It)


def test_a_reparse_directory_is_not_followed(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _w(home / "Application Data/secret.txt")
    _w(home / "Documents/시작 메뉴/x.lnk")
    _w(home / "Documents/a.txt")
    _patch_attrs(monkeypatch, {"Application Data": FILE_ATTRIBUTE_REPARSE_POINT | 0x10,
                               "시작 메뉴": FILE_ATTRIBUTE_REPARSE_POINT | 0x10})
    res = scan_home(home, exclude_names=[])
    assert _rels(home, res) == {"Documents/a.txt"}
    assert sum("리parse" in r for _, r in res.excluded) == 2


def test_a_reparse_file_is_skipped(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _w(home / "link.txt")
    _w(home / "real.txt")
    _patch_attrs(monkeypatch, {"link.txt": FILE_ATTRIBUTE_REPARSE_POINT | 0x20})
    assert _rels(home, scan_home(home, exclude_names=[])) == {"real.txt"}


def test_cloud_placeholders_are_skipped_without_reading(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _w(home / "OneDrive/online-only.docx")
    _w(home / "OneDrive/local.docx")
    _patch_attrs(monkeypatch, {"online-only.docx": FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS})
    res = scan_home(home, exclude_names=[])
    assert _rels(home, res) == {"OneDrive/local.docx"}
    assert any("클라우드" in r for _, r in res.excluded)


def test_a_real_symlink_is_not_followed(tmp_path):
    home = tmp_path / "h"
    outside = tmp_path / "outside"
    _w(outside / "elsewhere.txt")
    _w(home / "a.txt")
    try:
        (home / "linkdir").symlink_to(outside, target_is_directory=True)
        (home / "linkfile.txt").symlink_to(outside / "elsewhere.txt")
    except (OSError, NotImplementedError):
        pytest.skip("심볼릭 링크를 만들 권한이 없습니다")
    res = scan_home(home, exclude_names=[])
    assert _rels(home, res) == {"a.txt"}
    assert {p.name for p, _ in res.excluded} >= {"linkdir", "linkfile.txt"}


@pytest.mark.skipif(sys.platform != "win32", reason="정션은 Windows 전용")
def test_a_real_junction_is_not_followed(tmp_path):
    home = tmp_path / "h"
    target = home / "AppData" / "Roaming"
    _w(target / "deep.txt")
    _w(home / "Documents/a.txt")
    junction = home / "Documents" / "Application Data"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not junction.exists():
        pytest.skip(f"정션을 만들지 못했습니다: {r.stdout} {r.stderr}")
    res = scan_home(home, exclude_names=[])
    assert _rels(home, res) == {"Documents/a.txt"}
    assert any(p == junction for p, _ in res.excluded)


def test_unreadable_directory_is_reported_not_raised(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _w(home / "locked/x.txt")
    _w(home / "ok.txt")
    real = os.scandir

    def fake(path):
        if Path(path).name == "locked":
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(scan_mod.os, "scandir", fake)
    res = scan_home(home, exclude_names=[])
    assert _rels(home, res) == {"ok.txt"}
    assert [p.name for p, _ in res.unreadable] == ["locked"]


def test_iter_home_is_lazy(tmp_path, monkeypatch):
    """첫 파일을 내기 위해 홈 전체를 훑으면 66만 개를 다 들고 있게 됩니다."""
    home = tmp_path / "h"
    for d in ("a", "b", "c", "d"):
        _w(home / d / "f.txt")
    opened: list[str] = []
    real = os.scandir

    def spy(path):
        opened.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(scan_mod.os, "scandir", spy)
    it = iter_home(home, exclude_names=[])
    first = next(it)
    assert first[0] == home / "a" / "f.txt"
    assert len(opened) == 2, opened          # 홈과 a 만 열었습니다


def test_iter_home_order_is_deterministic_and_reports(tmp_path):
    home = tmp_path / "h"
    for rel in ("b/z.txt", "a/y.txt", "a.txt", "b/a.txt", "a/sub/q.txt", "AppData/x"):
        _w(home / rel)
    seen: list[tuple[Path, str]] = []
    order = [p.relative_to(home).as_posix() for p, _, _ in
             iter_home(home, exclude_names=[], on_excluded=lambda p, r: seen.append((p, r)))]
    assert order == ["a.txt", "a/y.txt", "a/sub/q.txt", "b/a.txt", "b/z.txt"]
    assert [p.name for p, _ in seen] == ["AppData"]


def test_a_real_lone_surrogate_file_goes_to_unreadable(tmp_path):
    """NTFS 는 짝 없는 서로게이트 이름을 허용합니다. 예외 대신 unreadable 로 가야 합니다."""
    home = tmp_path / "h"
    _w(home / "ok.txt")
    bad = home / "sub" / "bad\udc80.txt"
    try:
        _w(bad)
    except (OSError, UnicodeEncodeError, ValueError):
        pytest.skip("이 파일 시스템은 서로게이트 이름을 만들 수 없습니다")
    if not any("\udc80" in n for n in os.listdir(home / "sub")):
        pytest.skip("이름이 바뀌어 저장됐습니다")
    unreadable: list[tuple[Path, str]] = []
    got = [p.relative_to(home).as_posix()
           for p, _, _ in iter_home(home, exclude_names=[],
                                    on_unreadable=lambda p, r: unreadable.append((p, r)))]
    assert got == ["ok.txt"]
    assert len(unreadable) == 1 and "UTF-8" in unreadable[0][1]


def test_a_lone_surrogate_directory_is_not_descended(tmp_path, monkeypatch):
    home = tmp_path / "h"
    _w(home / "good/f.txt")
    _w(home / "evil/f.txt")
    real = os.scandir

    class _Renamed:
        def __init__(self, e):
            self._e = e
            self.name = "evil\udc81" if e.name == "evil" else e.name
            self.path = e.path

        def __getattr__(self, attr):
            return getattr(self._e, attr)

    class _It:
        def __init__(self, folder):
            self._it = real(folder)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            return (_Renamed(e) for e in self._it)

    monkeypatch.setattr(scan_mod.os, "scandir", _It)
    unreadable: list[tuple[Path, str]] = []
    got = [p.relative_to(home).as_posix() for p, _, _ in
           iter_home(home, exclude_names=[], on_unreadable=lambda p, r: unreadable.append((p, r)))]
    assert got == ["good/f.txt"]
    assert [p.name for p, _ in unreadable] == ["evil"]


def test_default_exclude_names_come_from_config():
    from gymhall.config import DEFAULTS

    assert DEFAULT_EXCLUDE_NAMES == DEFAULTS["backup"]["home"]["exclude_names"]
    assert DEFAULT_EXCLUDE_NAMES is not DEFAULTS["backup"]["home"]["exclude_names"]
