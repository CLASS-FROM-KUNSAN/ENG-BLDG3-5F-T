"""SoX 설치(필수 녹음 도구): 찾기 → winget → 공식 zip(해시 확인) → 바꿔 끼우기(롤백)."""

from __future__ import annotations

import hashlib
import io
import types
import zipfile
from pathlib import Path

import pytest

from syncnote import sox_fetch as S


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _pin(monkeypatch, data: bytes):
    monkeypatch.setattr(S, "SOX_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(S, "SOX_SIZE", len(data))


def _run(ok_paths: set[str]):
    def run(cmd, **_kw):
        good = str(cmd[0]) in ok_paths or any(str(cmd[0]).endswith(p) for p in ok_paths)
        return types.SimpleNamespace(returncode=0 if good else 1,
                                     stdout="sox:      SoX v14.4.2" if good else "", stderr="")
    return run


def test_candidates_include_tools_env_and_winget(tmp_path):
    root = tmp_path / "repo"
    (root / "tools" / "sox").mkdir(parents=True)
    (root / "tools" / "sox" / "sox.exe").write_bytes(b"x")
    wg = tmp_path / "local" / "Microsoft" / "WinGet" / "Packages" / "ChrisBagwell.SoX_abc" / "sox-14.4.2"
    wg.mkdir(parents=True)
    (wg / "sox.exe").write_bytes(b"x")
    got = [str(p) for p in S.candidates(root=root, env={"SOX": "C:/x/sox.exe",
                                                         "LOCALAPPDATA": str(tmp_path / "local")},
                                        which=lambda _n: None)]
    assert got[0] == str(Path("C:/x/sox.exe"))
    assert str(root / "tools" / "sox" / "sox.exe") in got and str(wg / "sox.exe") in got


def test_existing_sox_is_used_without_installing(tmp_path):
    (tmp_path / "tools" / "sox").mkdir(parents=True)
    exe = tmp_path / "tools" / "sox" / "sox.exe"
    exe.write_bytes(b"x")
    r = S.install(tmp_path / "tools", run=_run({str(exe)}), which=lambda _n: None, env={},
                  platform="win32", say=lambda _m: None)
    assert r.ok and r.sox == str(exe) and r.tried == [("찾기", f"있음: {exe}")]


def test_other_os_gets_the_install_command(tmp_path):
    r = S.install(tmp_path / "tools", run=_run(set()), which=lambda _n: None, env={},
                  platform="linux", say=lambda _m: None)
    assert not r.ok and S.LINUX_HINT in r.tried[-1][1]


def test_winget_fails_then_official_zip_with_pinned_hash(tmp_path, monkeypatch):
    data = _zip({"sox-14.4.2/sox.exe": b"EXE", "sox-14.4.2/zlib1.dll": b"DLL"})
    _pin(monkeypatch, data)
    tools = tmp_path / "tools"
    calls = []

    def run(cmd, **kw):
        calls.append(Path(cmd[0]).name)
        if Path(cmd[0]).name == "winget.exe":
            return types.SimpleNamespace(returncode=1, stdout="정책으로 막힘", stderr="")
        return _run({"sox.exe"})(cmd, **kw)
    r = S.install(tools, run=run, which=lambda n: "C:/w/winget.exe" if n == "winget" else None,
                  opener=lambda url: Resp(data), env={}, platform="win32", say=lambda _m: None)
    assert r.ok and Path(r.sox) == tools / "sox" / "sox.exe"
    assert (tools / "sox" / "zlib1.dll").read_bytes() == b"DLL"
    assert [s for s, _ in r.tried] == ["찾기", "winget", "공식 zip"]
    assert not (tools / ".sox-install").exists()                      # 임시 폴더 정리


def test_hash_mismatch_is_refused_and_old_sox_kept(tmp_path, monkeypatch):
    good = _zip({"sox-14.4.2/sox.exe": b"EXE"})
    _pin(monkeypatch, good)
    tools = tmp_path / "tools"
    (tools / "sox").mkdir(parents=True)
    (tools / "sox" / "keep.txt").write_bytes(b"old")
    evil = _zip({"sox-14.4.2/sox.exe": b"EVIL"})
    with pytest.raises(OSError, match="크기|SHA-256"):
        S.install_zip(tools, opener=lambda url: Resp(evil), run=_run({"sox.exe"}),
                      say=lambda _m: None)
    assert (tools / "sox" / "keep.txt").read_bytes() == b"old"       # 쓰던 것 그대로
    assert not (tools / ".sox-install").exists()


def test_unrunnable_download_rolls_back(tmp_path, monkeypatch):
    data = _zip({"sox-14.4.2/sox.exe": b"EXE"})
    _pin(monkeypatch, data)
    tools = tmp_path / "tools"
    (tools / "sox").mkdir(parents=True)
    (tools / "sox" / "keep.txt").write_bytes(b"old")
    with pytest.raises(OSError, match="실행되지"):
        S.install_zip(tools, opener=lambda url: Resp(data), run=_run(set()), say=lambda _m: None)
    assert (tools / "sox" / "keep.txt").exists()


def test_zip_slip_is_refused(tmp_path):
    z = tmp_path / "a.zip"
    z.write_bytes(_zip({"../evil/sox.exe": b"x"}))
    with pytest.raises(OSError, match="바깥"):
        S.extract(z, tmp_path / "out")


@pytest.mark.parametrize("url, ok", [
    ("https://sourceforge.net/projects/sox/files/x/download", True),
    ("https://twds.dl.sourceforge.net/project/sox/x.zip", True),
    ("http://downloads.sourceforge.net/x.zip", False),                 # https 만
    ("https://evil.example/sourceforge.net/x.zip", False),
    ("https://sourceforge.net.evil.example/x.zip", False),
])
def test_only_sourceforge_over_https(url, ok):
    assert S.host_ok(url) is ok


def test_terminal_finds_sox_through_installer_locations(monkeypatch):
    from syncnote.recorder import terminal as t

    monkeypatch.setattr(S, "find_sox", lambda **kw: "C:/repo/tools/sox/sox.exe")
    assert t.find_tool("sox") == "C:/repo/tools/sox/sox.exe"


def test_doctor_requires_sox(tmp_path, monkeypatch, capsys):
    from syncnote import cli
    from syncnote import config as C
    from syncnote.paths import Layout

    cfg = C.Config(data=C._deep_merge(C.DEFAULTS, {"recording": {"enabled": False}}))
    monkeypatch.setattr(cli, "_load", lambda _a: (cfg, Layout(tmp_path)))
    monkeypatch.setattr(S, "find_sox", lambda **kw: None)
    rep: dict = {"ok": True, "info": [], "config_error": None, "sections": [], "verdict": ""}
    cli._doctor(types.SimpleNamespace(json=False, config=None), rep)
    out = capsys.readouterr().out
    assert "[X] sox" in out and rep["ok"] is False
