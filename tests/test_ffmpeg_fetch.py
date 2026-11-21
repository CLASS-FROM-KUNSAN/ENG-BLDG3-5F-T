"""ffmpeg 를 릴리스 목록에서 받기 — 실패하면 이전 릴리스로 롤백.

네트워크·실제 ffmpeg 없이: 릴리스 JSON·zip·ffmpeg 출력을 가짜로 줍니다.
"""

import errno
import hashlib
import io
import json
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest

from syncnote import ffmpeg_fetch as F


@pytest.fixture(autouse=True)
def _no_real_gh_or_git(monkeypatch):
    """진짜 gh·git 을 부르지 않습니다(네트워크·로그인 상태에 따라 결과가 달라지므로).
    세 출처를 쓰는 시험은 which 와 run 을 직접 넘깁니다."""
    from syncnote import release_sources as RS

    monkeypatch.setattr(RS, "which", lambda _n: None)

GOOD_DEVICES = " D  dshow\n D  gdigrab         GDI API Windows frame grabber\n"
GOOD_ENCODERS = " V....D libx264  H.264\n A....D aac      AAC (Advanced Audio Coding)\n"


def _zip(top="ffmpeg-x", marker=b"ok") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/bin/ffmpeg.exe", marker)
        z.writestr(f"{top}/bin/ffprobe.exe", b"p")
    return buf.getvalue()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _asset(tag, data=b"", digest=None, kind="essentials_build.zip"):
    return {"name": f"ffmpeg-{tag}-{kind}", "size": len(data),
            "browser_download_url": f"https://dl/{tag}/{kind}",
            "digest": f"sha256:{_sha(data) if digest is None else digest}"}


def _release(tag, *assets, draft=False, pre=False, when="2026-11-01T00:00:00Z"):
    return {"tag_name": tag, "draft": draft, "prerelease": pre, "published_at": when,
            "assets": list(assets)}


class _Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def opener_for(routes: dict):
    """url -> bytes | Exception | list(차례로)."""
    calls = []

    def _open(url, timeout=30):
        calls.append(url)
        v = routes.get(url)
        if isinstance(v, list):
            v = v.pop(0)
        if v is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(v, BaseException):
            raise v
        return _Resp(v)
    _open.calls = calls
    return _open


def runner(bad: dict | None = None):
    """ffmpeg 출력 흉내. 파일 내용(marker)에 따라 기능을 뺍니다."""
    bad = bad or {}

    def run(cmd, **_kw):
        marker = Path(cmd[0]).read_bytes().decode()
        devices, encoders = GOOD_DEVICES, GOOD_ENCODERS
        for feature in bad.get(marker, []):
            devices = devices.replace(feature, "x")
            encoders = encoders.replace(feature, "x")
        return types.SimpleNamespace(stdout=devices if cmd[2] == "-devices" else encoders,
                                     stderr="", returncode=0)
    return run


# -- 후보 순서 ---------------------------------------------------------------
def test_candidates_stable_newest_first_then_nightly_and_skip_others():
    rels = [_release("2026-11-19-git-a", _asset("2026-11-19-git-a"), when="2026-11-19T00:00:00Z"),
            _release("9.1", _asset("9.1"), _asset("9.1", kind="full_build.zip"),
                     _asset("9.1", kind="essentials_build.7z")),
            _release("9.0.2", _asset("9.0.2")),
            _release("10.0", _asset("10.0"), draft=True),                   # 초안은 제외
            _release("9.10", _asset("9.10")),                               # 9.10 > 9.1
            _release("2026-11-20-git-b", _asset("2026-11-20-git-b"), when="2026-11-20T00:00:00Z")]
    order = [c.version for c in F.candidates(rels)]
    assert order == ["9.10", "9.1", "9.0.2", "2026-11-20-git-b", "2026-11-19-git-a"]
    c = F.candidates(rels)[1]
    assert c.name == "ffmpeg-9.1-essentials_build.zip" and len(c.sha256) == 64


# -- 내려받기 ------------------------------------------------------------------
def test_download_retries_transient_errors_then_checks_hash(tmp_path):
    data = _zip()
    c = F.Candidate("a.zip", "https://dl/a", "9.1", sha256=_sha(data))
    waits = []
    op = opener_for({"https://dl/a": [urllib.error.URLError("reset"),
                                      urllib.error.HTTPError("u", 503, "x", {}, None), data]})
    F.download(c, tmp_path / "a.zip", opener=op, sleep=waits.append, say=lambda _m: None)
    assert waits == [2, 4] and (tmp_path / "a.zip").read_bytes() == data


def test_download_gives_up_without_retry_on_404_and_on_bad_hash(tmp_path):
    waits = []
    with pytest.raises(F.CandidateFailed, match="내려받기 실패"):
        F.download(F.Candidate("a.zip", "https://dl/none", "9.1"), tmp_path / "a.zip",
                   opener=opener_for({}), sleep=waits.append, say=lambda _m: None)
    assert waits == []
    with pytest.raises(F.CandidateFailed, match="SHA-256"):
        F.download(F.Candidate("b.zip", "https://dl/b", "9.1", sha256="0" * 64),
                   tmp_path / "b.zip", opener=opener_for({"https://dl/b": b"tampered"}),
                   sleep=waits.append, say=lambda _m: None)
    assert not list(tmp_path.iterdir())                                  # 반쪽 파일 안 남김


def test_extract_rejects_paths_that_escape(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../evil.exe", b"x")
    (tmp_path / "e.zip").write_bytes(buf.getvalue())
    with pytest.raises(F.CandidateFailed, match="바깥"):
        F.extract(tmp_path / "e.zip", tmp_path / "x")
    assert not (tmp_path.parent / "evil.exe").exists()


def test_missing_features_reads_ffmpeg_output(tmp_path):
    exe = tmp_path / "ffmpeg.exe"
    exe.write_bytes(b"nogdi")
    got = F.missing_features(exe, run=runner({"nogdi": ["gdigrab", "libx264"]}))
    # 화면 캡처(gdigrab)는 Windows 에서만 필요
    assert got == (["gdigrab", "libx264"] if F.os.name == "nt" else ["libx264"])
    assert F.missing_features(tmp_path / "gone.exe") == ["실행 안 됨"]


# -- 전체: 롤백 --------------------------------------------------------------
def _routes(*specs):
    """(tag, zip bytes, digest 덮어쓰기) -> (releases, routes)."""
    rels, routes = [], {}
    for tag, data, digest in specs:
        a = _asset(tag, data, digest)
        rels.append(_release(tag, a))
        routes[a["browser_download_url"]] = data
    routes[F.RELEASES_API] = json.dumps(rels).encode()
    routes[F.GYAN_URL + ".sha256"] = None
    return routes


def test_rolls_back_to_older_release_until_one_works(tmp_path):
    routes = _routes(("9.1", _zip(marker=b"v91"), "0" * 64),              # 해시 틀림
                     ("9.0.2", _zip(marker=b"v902"), None),               # 기능 없음
                     ("9.0", _zip(marker=b"v90"), None))                  # 정상
    logs = []
    r = F.install(tmp_path, opener=opener_for(routes), run=runner({"v902": ["libx264"]}),
                  sleep=lambda _s: None, say=logs.append)
    assert r.ok and r.used.version == "9.0"
    assert [v for v, _ in r.tried] == ["9.1", "9.0.2"]
    assert "SHA-256" in r.tried[0][1] and "libx264" in r.tried[1][1]
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"v90"
    assert any("롤백: 9.0.2" in m for m in logs) and any("롤백: 9.0" in m for m in logs)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ffmpeg"]      # 임시 폴더 정리


def test_existing_good_ffmpeg_is_kept_without_downloading(tmp_path):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"have")
    op = opener_for({})
    r = F.install(tmp_path, opener=op, run=runner(), say=lambda _m: None)
    assert r.ok and op.calls == []


def test_broken_existing_ffmpeg_is_replaced_and_old_one_removed(tmp_path):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"old")
    routes = _routes(("9.1", _zip(marker=b"new"), None))
    r = F.install(tmp_path, opener=opener_for(routes), run=runner({"old": ["aac"]}),
                  say=lambda _m: None)
    assert r.ok and (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"new"
    assert not (tmp_path / "ffmpeg.old").exists()


def test_swap_failure_puts_the_old_ffmpeg_back(tmp_path, monkeypatch):
    (tmp_path / "ffmpeg" / "bin").mkdir(parents=True)
    (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"old")
    new = tmp_path / "new"
    (new / "bin").mkdir(parents=True)

    def boom(*_a, **_k):
        raise PermissionError("locked")
    monkeypatch.setattr(F.shutil, "move", boom)
    with pytest.raises(F.CandidateFailed, match="옮기지 못함"):
        F.swap_in(new, tmp_path)
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"old"


def test_list_unreachable_falls_back_to_gyan_dev(tmp_path):
    data = _zip(marker=b"gyan")
    routes = {F.RELEASES_API: urllib.error.URLError("blocked"),
              F.GYAN_URL + ".sha256": f"{_sha(data)} *ffmpeg-release-essentials.zip".encode(),
              F.GYAN_URL: data}
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert r.ok and r.used.version == "gyan.dev" and r.used.sha256 == _sha(data)


def test_everything_fails_reports_each_reason(tmp_path):
    routes = _routes(("9.1", _zip(marker=b"a"), "0" * 64), ("9.0", _zip(marker=b"b"), "0" * 64))
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert not r.ok and [v for v, _ in r.tried] == ["9.1", "9.0", "gyan.dev"]
    assert not (tmp_path / "ffmpeg").exists()


def test_disk_full_stops_instead_of_trying_older_releases(tmp_path, monkeypatch):
    routes = _routes(("9.1", _zip(), None), ("9.0", _zip(), None))
    real_open = Path.open

    def full(self, mode="r", *a, **k):
        if "w" in mode and self.name.endswith(".part"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, mode, *a, **k)
    monkeypatch.setattr(Path, "open", full)
    r = F.install(tmp_path, opener=opener_for(routes), run=runner(), say=lambda _m: None)
    assert not r.ok and len(r.tried) == 1 and "디스크" in r.tried[0][1]


# -- pip(imageio-ffmpeg) 경로 --------------------------------------------------
def _fake_imageio(monkeypatch, exe):
    import sys
    import types as _t

    mod = _t.ModuleType("imageio_ffmpeg")
    mod.get_ffmpeg_exe = lambda: str(exe)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", mod)


def test_pip_ffmpeg_used_only_when_it_has_needed_features(tmp_path, monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    exe = tmp_path / "ffmpeg-win-x86_64-v9.0.2.exe"
    exe.write_bytes(b"x")
    _fake_imageio(monkeypatch, exe)
    U.reset_cache()
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: [])
    assert U.pip_ffmpeg() == str(exe)
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: ["libx264"])
    assert U.pip_ffmpeg() == str(exe)                                   # 프로세스당 한 번(캐시)
    U.reset_cache()
    assert U.pip_ffmpeg() is None                                        # 기능 없음 -> 안 씀
    U.reset_cache()


def test_pip_ffmpeg_absent_module_is_skipped(monkeypatch):
    import sys

    from syncnote.recorder import ffmpeg_util as U

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)            # import 실패
    U.reset_cache()
    assert U.pip_ffmpeg() is None
    U.reset_cache()


def test_pip_install_reports_failure_and_rechecks_after_success(monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    calls = []
    fail = lambda cmd, **k: calls.append(cmd) or types.SimpleNamespace(returncode=1)  # noqa: E731
    assert F.pip_install(run=fail, say=lambda _m: None) == ""
    assert calls[0][1:4] == ["-m", "pip", "install"] and "imageio-ffmpeg>=0.5" in calls[0]
    resets = []
    monkeypatch.setattr(U, "reset_cache", lambda: resets.append(1))
    monkeypatch.setattr(U, "pip_ffmpeg", lambda: "C:/site/ffmpeg.exe")
    ok = lambda cmd, **k: types.SimpleNamespace(returncode=0)  # noqa: E731
    assert F.pip_install(run=ok, say=lambda _m: None) == "C:/site/ffmpeg.exe" and resets


def test_find_usable_rejects_a_found_ffmpeg_without_features(monkeypatch):
    from syncnote.recorder import ffmpeg_util as U

    monkeypatch.setattr(U, "find_ffmpeg", lambda: "C:/old/ffmpeg.exe")
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: ["libx264"])
    logs = []
    assert F.find_usable(say=logs.append) == "" and "FFMPEG" in logs[0]
    monkeypatch.setattr(F, "missing_features", lambda _p, **_k: [])
    assert F.find_usable(say=logs.append) == "C:/old/ffmpeg.exe"


# -- 원인별 자동 흐름 (--auto) -------------------------------------------------
def _rel(ok=False, tried=(), kinds=(), list_failed=False, exe=""):
    r = F.Result(ok, exe, used=F.Candidate("x", "u", "9.1") if ok else None)
    r.tried, r.kinds, r.list_failed = list(tried), list(kinds), list_failed
    return r


def _auto(tmp_path, **kw):
    base = {"find": lambda: "", "pip": lambda: "",
            "winget": lambda: F.WingetOutcome(reason="실패"), "release": lambda: _rel(),
            "from_zip": lambda _z: "", "say": lambda _m: None,
            "state_path": tmp_path / "state.json"}
    base.update(kw)
    return F.auto(tmp_path / "tools", **base)


def test_auto_stops_at_the_first_step_that_works(tmp_path):
    order = []
    r = _auto(tmp_path, find=lambda: order.append("find") or "",
              pip=lambda: order.append("pip") or "C:/pip/ffmpeg.exe",
              winget=lambda: order.append("winget") or F.WingetOutcome())
    assert r.ok and r.ffmpeg == "C:/pip/ffmpeg.exe" and order == ["find", "pip"]


def test_auto_prefers_a_zip_dropped_into_tools_before_the_network(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ffmpeg-9.1-essentials_build.zip").write_bytes(b"z")
    (tmp_path / "tools" / "notes.zip").write_bytes(b"z")                # 이름에 ffmpeg 없음
    seen = []
    r = _auto(tmp_path, from_zip=lambda z: seen.append(z.name) or "C:/t/ffmpeg.exe",
              pip=lambda: pytest.fail("네트워크 단계까지 가면 안 됨"))
    assert r.ok and seen == ["ffmpeg-9.1-essentials_build.zip"]


def test_permanent_winget_failure_is_remembered_and_skipped_next_time(tmp_path):
    calls = []
    perm = lambda: calls.append(1) or F.WingetOutcome(reason="winget 이 없음", permanent=True)  # noqa: E731
    _auto(tmp_path, winget=perm)
    r = _auto(tmp_path, winget=perm)
    assert calls == [1]                                                  # 두 번째는 안 부름
    assert [s.status for s in r.steps if s.name == "winget"] == ["skip"]
    _auto(tmp_path, winget=perm, retry_skipped=True)
    assert calls == [1, 1]


def test_transient_winget_failure_is_not_remembered(tmp_path):
    calls = []
    flaky = lambda: calls.append(1) or F.WingetOutcome(reason="종료 코드 1")  # noqa: E731
    _auto(tmp_path, winget=flaky)
    _auto(tmp_path, winget=flaky)
    assert calls == [1, 1]


def test_downloads_folder_zip_is_the_last_resort(tmp_path):
    dl = tmp_path / "Downloads"
    dl.mkdir()
    (dl / "ffmpeg-release-essentials.zip").write_bytes(b"z")
    r = _auto(tmp_path, downloads=dl, from_zip=lambda z: "C:/dl/ffmpeg.exe",
              release=lambda: _rel(tried=[("9.1", "내려받기 실패")], kinds=["net"]))
    assert r.ok and r.steps[-1].name.startswith("다운로드 폴더 zip")


def test_all_fail_lists_every_step_and_one_next_action(tmp_path, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    r = _auto(tmp_path, release=lambda: _rel(
        tried=[("9.1", "내려받기 실패: x"), ("gyan.dev", "내려받기 실패: y")],
        kinds=["net", "net"], list_failed=True))
    names = [s.name for s in r.steps]
    assert names == ["이미 있음", "pip (imageio-ffmpeg)", "winget", "릴리스 9.1", "릴리스 gyan.dev"]
    assert "HTTPS_PROXY" in r.next_action
    lines = []
    F.print_summary(r, say=lines.append)
    assert any(ln.startswith("다음 할 일:") for ln in lines)
    assert any("3. winget — 실패" in ln for ln in lines)


@pytest.mark.parametrize("kinds,reason,expect", [
    (["fatal"], "디스크 공간이 부족합니다", "디스크"),
    (["hash", "hash"], "SHA-256", "USB"),
    (["feature"], "기능 없음", "USB"),
    (["net"], "내려받기 실패", "HTTPS_PROXY"),
])
def test_next_action_follows_the_cause(kinds, reason, expect):
    steps = [F.Step("릴리스", "fail", reason)]
    assert expect in F.next_action(steps, kinds)


def test_next_action_with_proxy_set_asks_to_check_it():
    assert "proxy.school:8080" in F.next_action([F.Step("r", "fail", "x")], ["net"],
                                               proxy="http://proxy.school:8080")


def test_winget_missing_or_blocked_by_policy_is_permanent():
    w = F.winget_install(which=lambda _n: None, say=lambda _m: None)
    assert w.permanent and not w.ffmpeg
    blocked = lambda *a, **k: types.SimpleNamespace(  # noqa: E731
        returncode=1, stdout="This operation is disabled by Group Policy", stderr="")
    w = F.winget_install(which=lambda _n: "winget.exe", run=blocked, find=lambda: "",
                         say=lambda _m: None)
    assert w.permanent and "정책" in w.reason
    other = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="network", stderr="")  # noqa: E731
    assert not F.winget_install(which=lambda _n: "w", run=other, find=lambda: "",
                                say=lambda _m: None).permanent


def test_winget_nonzero_exit_but_installed_counts_as_success():
    already = lambda *a, **k: types.SimpleNamespace(  # noqa: E731
        returncode=0x8A15002B, stdout="Found an existing package already installed.", stderr="")
    w = F.winget_install(which=lambda _n: "w", run=already, find=lambda: "C:/wg/ffmpeg.exe",
                         say=lambda _m: None)
    assert w.ffmpeg == "C:/wg/ffmpeg.exe"


def test_install_from_zip_checks_optional_hash_and_features(tmp_path):
    data = _zip(marker=b"usb")
    z = tmp_path / "ffmpeg-usb.zip"
    z.write_bytes(data)
    dest = tmp_path / "tools"
    assert F.install_from_zip(z, dest, sha256="0" * 64, run=runner(), say=lambda _m: None) == ""
    assert F.install_from_zip(z, dest, run=runner({"usb": ["aac"]}), say=lambda _m: None) == ""
    exe = F.install_from_zip(z, dest, sha256=_sha(data), run=runner(), say=lambda _m: None)
    assert exe and (dest / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"usb"


# -- 받기 강화: 이어받기 · 크기 · 허용 주소 · 디스크 ------------------------------
class _RangeServer:
    """Range 를 아는 가짜 서버. cut_at 바이트에서 한 번 끊습니다."""

    def __init__(self, data, *, cut_at=None, honor_range=True, final_url=None):
        self.data, self.cut_at, self.honor = data, cut_at, honor_range
        self.final_url, self.ranges = final_url, []

    def __call__(self, url, timeout=30, headers=None):
        rng = (headers or {}).get("Range")
        self.ranges.append(rng)
        start = int(rng.split("=")[1].rstrip("-")) if rng and self.honor else 0
        body = self.data[start:]
        srv = self

        class Resp(io.BytesIO):
            status = 206 if (rng and srv.honor) else 200
            url = srv.final_url
            headers = {"Content-Length": str(len(body))}

            def read(self, n=-1):
                if srv.cut_at is not None and start + self.tell() >= srv.cut_at:
                    srv.cut_at = None
                    raise ConnectionResetError("끊김")
                return super().read(min(n, 1000) if n and n > 0 else n)
        return Resp(body)


def test_download_resumes_from_the_partial_file(tmp_path):
    data = bytes(range(256)) * 40
    c = F.Candidate("a.zip", "https://github.com/x", "9.1", sha256=_sha(data), size=len(data))
    srv = _RangeServer(data, cut_at=3000)
    F.download(c, tmp_path / "a.zip", opener=srv, sleep=lambda _s: None, say=lambda _m: None)
    assert (tmp_path / "a.zip").read_bytes() == data
    assert srv.ranges == [None, "bytes=3000-"]


def test_server_ignoring_range_restarts_cleanly(tmp_path):
    data = bytes(range(256)) * 40
    c = F.Candidate("a.zip", "https://github.com/x", "9.1", sha256=_sha(data), size=len(data))
    (tmp_path / "a.zip.part").write_bytes(data[:500])
    srv = _RangeServer(data, honor_range=False)
    logs = []
    F.download(c, tmp_path / "a.zip", opener=srv, sleep=lambda _s: None, say=logs.append)
    assert (tmp_path / "a.zip").read_bytes() == data and any("처음부터" in m for m in logs)


def test_size_mismatch_is_rejected_before_hashing(tmp_path, monkeypatch):
    data = b"x" * 100
    c = F.Candidate("a.zip", "https://github.com/x", "9.1", sha256=_sha(data), size=999)
    monkeypatch.setattr(F, "_sha256_file", lambda _p: pytest.fail("크기가 다르면 해시를 안 셈"))
    with pytest.raises(F.CandidateFailed, match="크기") as exc:
        F.download(c, tmp_path / "a.zip", opener=_RangeServer(data), sleep=lambda _s: None,
                   say=lambda _m: None)
    assert exc.value.kind == "hash" and not list(tmp_path.iterdir())


def test_redirect_to_a_host_outside_the_list_is_refused(tmp_path):
    data = b"x" * 100
    c = F.Candidate("a.zip", "https://github.com/x", "9.1", size=len(data))
    srv = _RangeServer(data, final_url="https://evil.example/a.zip")
    waits = []
    with pytest.raises(F.CandidateFailed, match="허용 밖"):
        F.download(c, tmp_path / "a.zip", opener=srv, sleep=waits.append, say=lambda _m: None)
    assert waits == [] and not list(tmp_path.iterdir())                  # 다시 안 함, 안 남김
    guard = F._GuardRedirect()
    with pytest.raises(F.HostNotAllowed):
        guard.redirect_request(None, None, 302, "", {}, "http://github.com/a")
    assert F.host_ok("https://release-assets.githubusercontent.com/x")
    assert not F.host_ok("https://github.com.evil.example/x")


def test_disk_is_checked_before_downloading(tmp_path):
    with pytest.raises(F.Fatal, match="디스크"):
        F.check_disk(tmp_path, 100_000_000, free=lambda _p: 200_000_000)  # 2.5배 = 250MB 필요
    F.check_disk(tmp_path, 100_000_000, free=lambda _p: 300_000_000)
    F.check_disk(tmp_path, 0, free=lambda _p: 0)                          # 크기 모르면 안 봄

    routes = _routes(("9.1", _zip(), None), ("9.0", _zip(), None))
    op = opener_for(routes)
    r = F.install(tmp_path / "t", opener=op, run=runner(), say=lambda _m: None,
                  disk_free=lambda _p: 10)
    assert not r.ok and r.kinds == ["fatal"] and "디스크" in r.tried[0][1]
    assert not any("/dl/" in u for u in op.calls)                        # 받기 전에 멈춤


def test_partial_download_survives_to_the_next_run(tmp_path):
    data = _zip(marker=b"resumed") + b"\0" * 5000
    sha = _sha(data)
    a = _asset("9.1", data)
    url = a["browser_download_url"]
    srv = _RangeServer(data, cut_at=2000)

    def first(u, timeout=30, headers=None):
        if u == F.RELEASES_API:
            return _Resp(json.dumps([_release("9.1", a)]).encode())
        if u == url:
            return srv(u, timeout, headers)
        raise urllib.error.HTTPError(u, 404, "nf", {}, None)

    fail_net = {"n": 0}

    def flaky(u, timeout=30, headers=None):
        if u == url and fail_net["n"] < 99:
            fail_net["n"] += 1
            if fail_net["n"] == 1:
                return srv(u, timeout, headers)                             # 2000 에서 끊김
            raise urllib.error.URLError("망 끊김")
        return first(u, timeout, headers)
    r1 = F.install(tmp_path, opener=flaky, run=runner(), sleep=lambda _s: None,
                   say=lambda _m: None)
    assert not r1.ok
    part = tmp_path / "_downloads" / f"{sha[:12]}-{a['name']}.part"
    assert part.exists() and part.stat().st_size == 2000                    # 다음 실행으로 남김

    srv.ranges.clear()
    r2 = F.install(tmp_path, opener=first, run=runner(), say=lambda _m: None)
    assert r2.ok and srv.ranges == ["bytes=2000-"]
    assert not (tmp_path / "_downloads").exists()                           # 끝나면 정리


def test_completed_download_is_reused_and_failed_one_is_removed(tmp_path):
    data = _zip(marker=b"kept")
    a = _asset("9.1", data)
    keep = tmp_path / "_downloads"
    keep.mkdir()
    (keep / f"{_sha(data)[:12]}-{a['name']}").write_bytes(data)
    routes = {F.RELEASES_API: json.dumps([_release("9.1", a)]).encode(),
              F.GYAN_URL + ".sha256": None}
    op = opener_for(routes)
    r = F.install(tmp_path, opener=op, run=runner(), say=lambda _m: None)
    assert r.ok and a["browser_download_url"] not in op.calls               # 다시 안 받음

    bad = _zip(marker=b"nofeat")
    b = _asset("9.0", bad)
    routes = {F.RELEASES_API: json.dumps([_release("9.0", b)]).encode(),
              b["browser_download_url"]: bad, F.GYAN_URL + ".sha256": None}
    r = F.install(tmp_path / "t2", opener=opener_for(routes),
                  run=runner({"nofeat": ["aac"]}), say=lambda _m: None)
    assert not r.ok and not list((tmp_path / "t2" / "_downloads").glob("*"))  # 기능 실패 -> 지움
