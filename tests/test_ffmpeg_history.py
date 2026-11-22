"""ffmpeg 설치 이력과 '같은 실패 반복' 막기. 진짜 네트워크·gh·git 을 쓰지 않습니다."""

import hashlib
import io
import json
import types
import urllib.error
import zipfile
from datetime import datetime, timedelta

import pytest

from syncnote import ffmpeg_fetch as F
from syncnote import ffmpeg_history as H
from syncnote import release_sources as RS

T0 = datetime(2026, 11, 22, 9, 0)


@pytest.fixture(autouse=True)
def _no_real_tools(monkeypatch):
    monkeypatch.setattr(RS, "which", lambda _n: None)


def _run(at, **sources):
    """sources: name=(ok, note)."""
    return {"at": at.isoformat(), "sources": {n: {"ok": ok, "note": note}
                                              for n, (ok, note) in sources.items()},
            "tried": [], "ok": False}


# -- 기록 ----------------------------------------------------------------------
def test_record_keeps_last_20_and_update_keeps_other_keys(tmp_path):
    p = tmp_path / "s.json"
    H.update(p, "winget", {"skip": True})
    for i in range(25):
        H.record(p, {"at": f"2026-11-22T09:{i:02d}:00", "n": i})
    data = H.load(p)
    assert [r["n"] for r in data["runs"]] == list(range(5, 25))
    assert data["winget"] == {"skip": True}
    H.update(p, "winget", {"skip": False})
    assert len(H.load(p)["runs"]) == 20


# -- 건너뛰기 규칙 -------------------------------------------------------------
def test_api_rate_limit_is_skipped_until_its_reset_time():
    data = {"runs": [_run(T0, API=(False, "API 한도 초과 (09:40에 풀림)"))]}
    assert "09:40" in H.skips(data, T0 + timedelta(minutes=10))["API"]
    assert "API" not in H.skips(data, T0 + timedelta(minutes=41))
    late = {"runs": [_run(T0.replace(hour=23, minute=50), API=(False, "API 한도 초과 (00:10에 풀림)"))]}
    assert "API" in H.skips(late, T0.replace(hour=23, minute=59))        # 자정 넘어 풀림


def test_three_failures_in_a_row_skip_that_source_for_24_hours():
    runs = [_run(T0 + timedelta(hours=i), git=(False, "git ls-remote 실패: timeout")) for i in range(3)]
    last = T0 + timedelta(hours=2)
    assert "git" in H.skips({"runs": runs}, last + timedelta(hours=1))
    assert "git" not in H.skips({"runs": runs}, last + timedelta(hours=25))
    assert "git" not in H.skips({"runs": runs[:2]}, last)                  # 두 번은 아직
    ok_between = runs[:2] + [_run(last, git=(True, "태그"))] + runs[2:]
    assert "git" not in H.skips({"runs": ok_between[:3]}, last)


def test_cheap_reasons_skipped_runs_and_rate_limits_do_not_count():
    cheap = [_run(T0 + timedelta(minutes=i), gh=(False, "gh 로그인 안 됨")) for i in range(3)]
    assert "gh" not in H.skips({"runs": cheap}, T0 + timedelta(minutes=5))
    limits = [_run(T0 + timedelta(hours=i), API=(False, f"API 한도 초과 ({9 + i:02d}:10에 풀림)"))
              for i in range(3)]
    assert "API" not in H.skips({"runs": limits}, T0 + timedelta(hours=3))  # 풀린 뒤엔 다시 부름
    gh = [_run(T0 + timedelta(hours=i), gh=(False, "gh 계정은 있으나 확인 실패")) for i in range(2)]
    gh.insert(1, _run(T0 + timedelta(minutes=30), gh=(False, "건너뜀: x")))  # 건너뛴 실행은 셈 밖
    gh.append(_run(T0 + timedelta(hours=3), gh=(False, "gh 계정은 있으나 확인 실패")))
    assert "gh" in H.skips({"runs": gh}, T0 + timedelta(hours=4))


def test_bad_builds_come_from_feature_failures_only():
    data = {"runs": [{"at": T0.isoformat(), "tried": [
        {"version": "9.1", "reason": "녹화에 필요한 기능 없음: gdigrab", "kind": "feature",
         "sha256": "a" * 64},
        {"version": "9.0", "reason": "내려받기 실패", "kind": "net", "sha256": "b" * 64}]}]}
    assert list(H.bad_builds(data)) == ["a" * 64]


# -- install 과 이어서 ------------------------------------------------------------
def _zip(marker):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ffmpeg-x/bin/ffmpeg.exe", marker)
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _asset(tag, data):
    return {"tag_name": tag, "assets": [{
        "name": RS.asset_name(tag), "size": len(data), "browser_download_url": f"https://dl/{tag}",
        "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}


class Net:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, timeout=30, headers=None):
        self.calls.append(url)
        v = self.routes.get(url)
        if isinstance(v, BaseException):
            raise v
        if v is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        return _Resp(v)


def _ffmpeg(bad=()):
    def run(cmd, **_kw):
        marker = open(cmd[0], "rb").read()  # noqa: SIM115
        lacks = marker in bad
        # 빠지는 기능은 libx264 — 모든 OS 에서 필요(gdigrab 은 Windows 에서만 필요해서
        # 리눅스 CI 에선 빠져도 통과함)
        return types.SimpleNamespace(returncode=0, stderr="", stdout=(
            " D  gdigrab  x\n" if cmd[2] == "-devices"
            else (" A aac x\n" if lacks else " V libx264 x\n A aac x\n")))
    return run


def test_api_rate_limit_is_remembered_and_the_next_run_does_not_call_it(tmp_path):
    limited = urllib.error.HTTPError(F.RELEASES_API, 403, "rate",
                                     {"X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int((T0 + timedelta(minutes=40)).timestamp()))},
                                     None)
    net = Net({F.RELEASES_API: limited})
    r1 = F.install(tmp_path, opener=net, run=_ffmpeg(), say=lambda _m: None, now=lambda: T0)
    assert not r1.ok and F.RELEASES_API in net.calls
    run = H.load(tmp_path / F.STATE_NAME)["runs"][-1]
    assert "한도 초과" in run["sources"]["API"]["note"]

    net.calls.clear()
    logs = []
    F.install(tmp_path, opener=net, run=_ffmpeg(), say=logs.append,
              now=lambda: T0 + timedelta(minutes=10))
    assert F.RELEASES_API not in net.calls and any("API 은 이번에 건너뜁니다" in m for m in logs)

    net.calls.clear()
    F.install(tmp_path, opener=net, run=_ffmpeg(), say=lambda _m: None, retry_skipped=True,
              now=lambda: T0 + timedelta(minutes=11))
    assert F.RELEASES_API in net.calls                                      # --retry-skipped


def test_build_without_needed_features_is_not_downloaded_again(tmp_path):
    bad, good = _zip(b"nofeat"), _zip(b"good")
    routes = {F.RELEASES_API: json.dumps([_asset("9.1", bad), _asset("9.0", good)]).encode(),
              "https://dl/9.1": bad, "https://dl/9.0": good}
    net = Net(routes)
    r1 = F.install(tmp_path, opener=net, run=_ffmpeg(bad=(b"nofeat",)), say=lambda _m: None,
                   now=lambda: T0)
    assert r1.ok and r1.used.version == "9.0" and r1.tried[0][0] == "9.1"
    assert H.bad_builds(H.load(tmp_path / F.STATE_NAME))

    net.calls.clear()
    logs = []
    r2 = F.install(tmp_path, opener=net, run=_ffmpeg(bad=(b"nofeat",)), say=logs.append,
                   force=True, now=lambda: T0 + timedelta(days=1))
    assert r2.ok and "https://dl/9.1" not in net.calls
    assert any("9.1 건너뜀" in m for m in logs)


def test_history_cli_shows_runs_and_current_skips(tmp_path, capsys):
    p = tmp_path / F.STATE_NAME
    H.record(p, _run(datetime.now(), API=(False, "API 한도 초과 (23:59에 풀림)")))
    H.update(p, "winget", {"skip": True, "reason": "winget 이 없음"})
    assert F.main(["--history", "--dest", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "실패" in out and "지금 건너뛰는 것" in out and "winget 이 없음" in out


def test_all_three_skipped_means_try_again_except_a_timed_rate_limit(tmp_path):
    p = tmp_path / F.STATE_NAME
    for i in range(3):
        H.record(p, _run(T0 + timedelta(minutes=i), API=(False, "API 실패: 연결 거부"),
                         gh=(False, "gh 계정은 있으나 확인 실패"), git=(False, "git 실패")))
    net = Net({F.RELEASES_API: urllib.error.URLError("x")})
    logs = []
    F.install(tmp_path, opener=net, run=_ffmpeg(), say=logs.append,
              now=lambda: T0 + timedelta(minutes=10))
    assert F.RELEASES_API in net.calls and any("다시 시도합니다" in m for m in logs)

    H.record(p, _run(T0 + timedelta(minutes=20), API=(False, "API 한도 초과 (09:50에 풀림)"),
                     gh=(False, "gh 계정은 있으나 확인 실패"), git=(False, "git 실패")))
    net.calls.clear()
    F.install(tmp_path, opener=net, run=_ffmpeg(), say=lambda _m: None,
              now=lambda: T0 + timedelta(minutes=25))
    assert F.RELEASES_API not in net.calls                                   # 한도는 지킴
