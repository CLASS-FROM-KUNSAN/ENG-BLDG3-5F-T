"""릴리스 목록을 API · gh · git 세 출처에서 함께 읽어 합치기.

진짜 gh·git·네트워크를 쓰지 않습니다. which 와 run 을 가짜로 넘깁니다.
"""

import hashlib
import io
import json
import types
import urllib.error
import zipfile

import pytest

from syncnote import ffmpeg_fetch as F
from syncnote import release_sources as RS

A = "a" * 64
B = "b" * 64


# -- 정규화 --------------------------------------------------------------------
@pytest.mark.parametrize("raw,tag", [
    ("refs/tags/9.1", "9.1"), ("refs/tags/9.1^{}", "9.1"), ("v9.0.2", "9.0.2"),
    ("2026-11-19-git-01ca75d350", "2026-11-19-git-01ca75d350"), ("vintage", "vintage")])
def test_normalize_tag(raw, tag):
    assert RS.normalize_tag(raw) == tag


def test_classify_keeps_only_release_versions():
    assert RS.classify("9.10") == ("stable", (9, 10))
    assert RS.classify("2026-11-19-git-01ca75d350") == ("nightly", (2026, 11, 19))
    assert RS.classify("tools-2020-10-28-git-81d4b8fb3c") is None
    assert RS.classify("2026-13-40-git-abc") is None                  # 없는 날짜
    assert RS.classify("latest") is None


@pytest.mark.parametrize("raw,ok", [
    ("sha256:" + A.upper(), A), (A + " *ffmpeg-release-essentials.zip\n", A), (A, A),
    ("sha256:xyz", ""), ("", ""), ("md5:" + A[:32], "")])
def test_norm_sha(raw, ok):
    assert RS.norm_sha(raw) == ok


# -- 합치기·교차 검증·정렬 ------------------------------------------------------
def _api(tag, sha, draft=False):
    return {"tag_name": tag, "draft": draft, "published_at": "2026-11-20T00:00:00Z",
            "assets": [{"name": RS.asset_name(tag), "size": 10,
                        "browser_download_url": f"https://dl/{tag}", "digest": f"sha256:{sha}"}]}


def _book(api=(), gh_list=(), gh_assets=None, git=""):
    book = {}
    RS.add_api(book, list(api))
    RS.add_gh_list(book, list(gh_list))
    for tag, sha in (gh_assets or {}).items():
        RS.add_gh_assets(book, tag, [{"name": RS.asset_name(tag), "digest": f"sha256:{sha}",
                                      "url": f"https://gh/{tag}", "size": 10}])
    RS.add_git(book, git)
    return book


def test_api_and_gh_agreeing_is_verified_by_both_and_disagreeing_is_dropped():
    book = _book(api=[_api("9.1", A), _api("9.0.2", A)],
                 gh_list=[{"tagName": "9.1"}, {"tagName": "9.0.2"}],
                 gh_assets={"9.1": A, "9.0.2": B})
    RS.cross_check(book)
    assert book["9.1"].verified_by == ["api", "gh"] and book["9.1"].sha256 == A
    assert book["9.0.2"].conflict and book["9.0.2"].sha256 == ""
    assert [e.tag for e in RS.ordered(book)] == ["9.1"]                 # 불일치는 후보에서 뺌


def test_git_only_tags_get_a_built_url_and_stay_out_unless_allowed():
    git = ("x\trefs/tags/9.1\nx\trefs/tags/9.1^{}\nx\trefs/tags/8.1.2\n"
           "x\trefs/tags/tools-2020-10-28-git-81d4b8fb3c\n")
    book = _book(api=[_api("9.1", A)], git=git)
    RS.cross_check(book)
    e = book["8.1.2"]
    assert e.synthesized and e.url == RS.asset_url("8.1.2") and e.sources == {"git"}
    assert "tools-2020-10-28-git-81d4b8fb3c" not in book
    assert [x.tag for x in RS.ordered(book)] == ["9.1"]
    assert [x.tag for x in RS.ordered(book, allow_unverified=True)] == ["9.1", "8.1.2"]


def test_gyan_hash_is_borrowed_only_by_the_newest_stable_without_one():
    book = _book(git="x\trefs/tags/9.1\nx\trefs/tags/9.0.2\nx\trefs/tags/2026-11-19-git-ab\n")
    RS.cross_check(book, gyan_sha=A)
    assert book["9.1"].verified_by == ["gyan.dev"]
    assert book["9.0.2"].sha256 == "" and book["2026-11-19-git-ab"].sha256 == ""
    assert [e.tag for e in RS.ordered(book)] == ["9.1"]

    book = _book(api=[_api("9.1", B)], git="x\trefs/tags/9.1\n")       # 이미 있으면 안 빌림
    RS.cross_check(book, gyan_sha=A)
    assert book["9.1"].sha256 == B and "gyan.dev" not in book["9.1"].sha


def test_order_is_verified_stable_then_verified_nightly_then_unverified():
    book = _book(api=[_api("9.0", A), _api("9.1", A), _api("2026-11-19-git-ab", A),
                      _api("10.0", A, draft=True)],
                 git="x\trefs/tags/8.0\nx\trefs/tags/2026-11-20-git-cd\n")
    RS.cross_check(book)
    assert [e.tag for e in RS.ordered(book, allow_unverified=True)] == [
        "9.1", "9.0", "2026-11-19-git-ab", "8.0", "2026-11-20-git-cd"]


# -- 출처 읽기 ------------------------------------------------------------------
class Tools:
    """gh·git 흉내. 부른 명령을 기록합니다."""

    def __init__(self, *, gh=True, logged_in=True, git=True, gh_list=(), gh_assets=None,
                 ls_remote="", fail=()):
        self.calls = []
        self.have = {"gh": gh, "git": git}
        self.logged_in, self.gh_list, self.gh_assets = logged_in, list(gh_list), gh_assets or {}
        self.ls_remote, self.fail = ls_remote, set(fail)

    def which(self, name):
        return name if self.have.get(name) else None

    def run(self, cmd, timeout=60):
        self.calls.append(cmd)
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "gh" and cmd[1:3] == ["auth", "status"]:
            ok.returncode = 0 if self.logged_in else 1
        elif cmd[0] == "gh" and cmd[1:3] == ["release", "list"]:
            ok.stdout = json.dumps(self.gh_list)
        elif cmd[0] == "gh" and cmd[1:3] == ["release", "view"]:
            tag = cmd[3]
            ok.stdout = json.dumps({"assets": [{"name": RS.asset_name(tag), "size": 10,
                                                "url": f"https://gh/{tag}",
                                                "digest": f"sha256:{self.gh_assets.get(tag, '')}"}]})
        elif cmd[0] == "git":
            ok.stdout = self.ls_remote
        if cmd[0] in self.fail:
            ok.returncode, ok.stderr = 1, "boom"
        return ok


def _collect(monkeypatch, tools, *, api=None, gyan="", allow=False):
    monkeypatch.setattr(RS, "which", tools.which)

    def api_fn():
        if isinstance(api, Exception):
            raise api
        return api or []
    return RS.collect(api=api_fn, gyan_sha=lambda: gyan, run=tools.run,
                      allow_unverified=allow, parallel=False)


def test_collect_reads_all_three_and_reports_each(monkeypatch):
    tools = Tools(gh_list=[{"tagName": "9.1"}], gh_assets={"9.1": A},
                  ls_remote="x\trefs/tags/9.1\nx\trefs/tags/tools-x\n")
    entries, reports = _collect(monkeypatch, tools, api=[_api("9.1", A)])
    assert [e.tag for e in entries] == ["9.1"] and entries[0].verified_by == ["api", "gh"]
    assert entries[0].sources == {"api", "gh", "git"}
    assert [(r.name, r.ok) for r in reports][:3] == [("API", True), ("gh", True), ("git", True)]
    assert "태그 2개 (릴리스 버전 1개)" in reports[2].note


def test_gh_missing_or_logged_out_is_skipped_without_prompting(monkeypatch):
    tools = Tools(gh=False)
    _e, reports = _collect(monkeypatch, tools, api=[_api("9.1", A)])
    assert (reports[1].name, reports[1].ok, reports[1].note) == ("gh", False, "gh 없음")
    tools = Tools(logged_in=False)
    _e, reports = _collect(monkeypatch, tools, api=[_api("9.1", A)])
    assert reports[1].note == "gh 로그인 안 됨"
    gh_cmds = [c for c in tools.calls if c[0] == "gh"]
    assert gh_cmds == [["gh", "auth", "status"]]                         # 로그인을 시키지 않음


def test_one_source_failing_does_not_stop_the_others(monkeypatch):
    tools = Tools(gh_list=[{"tagName": "9.1"}], gh_assets={"9.1": A},
                  ls_remote="x\trefs/tags/9.1\n", fail={"git"})
    entries, reports = _collect(monkeypatch, tools, api=urllib.error.URLError("rate limit"))
    assert [e.tag for e in entries] == ["9.1"] and entries[0].verified_by == ["gh"]
    assert [(r.name, r.ok) for r in reports][:3] == [("API", False), ("gh", True), ("git", False)]


def test_conflict_is_reported(monkeypatch):
    tools = Tools(gh_list=[{"tagName": "9.1"}], gh_assets={"9.1": B}, git=False)
    entries, reports = _collect(monkeypatch, tools, api=[_api("9.1", A)])
    assert entries == [] and reports[-1].name == "교차 검증" and "9.1" in reports[-1].note


# -- ffmpeg_fetch 와 이어서 ------------------------------------------------------
def _zip(marker=b"ok"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ffmpeg-x/bin/ffmpeg.exe", marker)
    return buf.getvalue()


class _Resp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _opener(routes):
    def op(url, timeout=30):
        v = routes.get(url)
        if v is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(v, BaseException):
            raise v
        return _Resp(v)
    return op


def _ffmpeg_ok(cmd, **_kw):
    return types.SimpleNamespace(
        returncode=0, stderr="",
        stdout=" D  gdigrab  x\n" if cmd[2] == "-devices" else " V libx264 x\n A aac x\n")


def test_api_blocked_and_no_gh_still_installs_via_git_plus_gyan_hash(tmp_path, monkeypatch):
    data = _zip(b"from-git")
    sha = hashlib.sha256(data).hexdigest()
    tools = Tools(gh=False, ls_remote="x\trefs/tags/9.1\nx\trefs/tags/9.0\n")
    monkeypatch.setattr(RS, "which", tools.which)
    routes = {F.RELEASES_API: urllib.error.URLError("rate limit"),
              F.GYAN_URL + ".sha256": f"{sha} *x.zip".encode(), RS.asset_url("9.1"): data}
    r = F.install(tmp_path, opener=_opener(routes), run=_ffmpeg_ok, tool_run=tools.run,
                  say=lambda _m: None)
    assert r.ok and r.used.version == "9.1" and r.used.synthesized
    assert r.used.verified_by == ("gyan.dev",)
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"from-git"
    assert any(s.startswith("git 됨") for s in r.sources)


def test_urllib_failure_falls_back_to_gh_download_for_the_same_candidate(tmp_path, monkeypatch):
    data = _zip(b"via-gh")
    sha = hashlib.sha256(data).hexdigest()
    tools = Tools(gh_list=[{"tagName": "9.1"}], gh_assets={"9.1": sha}, git=False)
    real_run = tools.run

    def run(cmd, timeout=60):
        if cmd[:3] == ["gh", "release", "download"]:
            (tmp_path / "dl").mkdir(exist_ok=True)
            out = __import__("pathlib").Path(cmd[cmd.index("-D") + 1]) / cmd[cmd.index("-p") + 1]
            out.write_bytes(data)
            tools.calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(cmd, timeout)
    monkeypatch.setattr(RS, "which", tools.which)
    routes = {F.RELEASES_API: json.dumps([_api("9.1", sha)]).encode(),
              "https://dl/9.1": urllib.error.URLError("blocked")}
    r = F.install(tmp_path, opener=_opener(routes), run=_ffmpeg_ok, tool_run=run,
                  sleep=lambda _s: None, say=lambda _m: None)
    assert r.ok and r.used.version == "9.1"
    assert any(c[:3] == ["gh", "release", "download"] for c in tools.calls)
    assert (tmp_path / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"via-gh"


def test_unverified_candidates_need_the_flag_and_a_real_file(tmp_path, monkeypatch):
    tools = Tools(gh=False, ls_remote="x\trefs/tags/8.1.2\nx\trefs/tags/8.0\n")
    monkeypatch.setattr(RS, "which", tools.which)
    data = _zip(b"unverified")
    routes = {F.RELEASES_API: urllib.error.URLError("x"), RS.asset_url("8.0"): data}
    r = F.install(tmp_path, opener=_opener(routes), run=_ffmpeg_ok, tool_run=tools.run,
                  sleep=lambda _s: None, say=lambda _m: None)
    assert not r.ok and [v for v, _ in r.tried] == ["gyan.dev"]          # 기본: 해시 없는 건 안 씀

    r = F.install(tmp_path, opener=_opener(routes), run=_ffmpeg_ok, tool_run=tools.run,
                  sleep=lambda _s: None, say=lambda _m: None, allow_unverified=True)
    assert r.ok and r.used.version == "8.0" and r.used.sha256 == ""
    assert r.tried[0][0] == "8.1.2" and "파일이 없음" in r.tried[0][1]   # 만든 주소에 없음 -> 롤백


# -- 2단계 보강 · 한도 · 신뢰 등급 ----------------------------------------------
def test_api_error_tells_limit_from_block():
    lim = RS.api_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1795260000"})
    assert isinstance(lim, RS.ApiLimited) and "에 풀림" in str(lim) and len(lim.reset) == 5
    assert isinstance(RS.api_error(429, {}), RS.ApiLimited)
    assert isinstance(RS.api_error(403, {"X-RateLimit-Remaining": "41"}), RS.ApiBlocked)
    assert RS.api_error(404, {}) is None


def test_api_get_reads_quota_and_raises_limited(monkeypatch):
    class Resp(io.BytesIO):
        headers = {"X-RateLimit-Remaining": "57", "X-RateLimit-Limit": "60"}
    ok = F._api_get("u", opener=lambda *_a, **_k: Resp(b"[]"))
    assert (ok.data, ok.remaining, ok.limit) == ([], 57, 60)

    def limited(*_a, **_k):
        raise urllib.error.HTTPError("u", 403, "rate", {"X-RateLimit-Remaining": "0"}, None)
    with pytest.raises(RS.ApiLimited):
        F._api_get("u", opener=limited)


def test_trust_levels():
    book = _book(api=[_api("9.1", A), _api("9.0", A), _api("8.0", A)],
                 gh_assets={"9.1": A, "8.0": B}, git="x\trefs/tags/7.0\n")
    book["9.1"].sources.add("gh")
    RS.cross_check(book)
    assert {t: e.trust for t, e in book.items()} == {"9.1": 3, "9.0": 2, "8.0": 0, "7.0": 1}


def _phase2(monkeypatch, *, git, api_page=(), tag_db=None, remaining=50, gh=False, max_tries=6,
            gh_assets=None, gh_list=()):
    tools = Tools(gh=gh, gh_list=list(gh_list), gh_assets=gh_assets or {}, ls_remote=git)
    monkeypatch.setattr(RS, "which", tools.which)
    asked = []
    left = {"n": remaining}

    def api_tag(tag):
        asked.append(tag)
        left["n"] -= 1
        if isinstance(tag_db, Exception):
            raise tag_db
        rel = (tag_db or {}).get(tag)
        return RS.ApiResult(rel, left["n"], 60)
    entries, reports = RS.collect(api=lambda: RS.ApiResult(list(api_page), remaining, 60),
                                  api_tag=api_tag, gyan_sha=lambda: "", run=tools.run,
                                  max_tries=max_tries, parallel=False)
    return entries, reports, asked, tools


def test_git_only_stable_versions_get_hashes_by_tag_lookup(monkeypatch):
    git = "x\trefs/tags/9.1\nx\trefs/tags/8.1.2\nx\trefs/tags/6.1.1\n"
    db = {"8.1.2": _api("8.1.2", B), "6.1.1": _api("6.1.1", "")}       # 6.1.1: 옛 파일, 해시 칸 빔
    entries, reports, asked, _t = _phase2(monkeypatch, git=git, api_page=[_api("9.1", A)],
                                          tag_db=db)
    assert asked == ["8.1.2", "6.1.1"]                                   # 해시 없는 것만, 9.1 은 안 물음
    assert [(e.tag, e.trust) for e in entries] == [("9.1", 2), ("8.1.2", 2)]
    assert "6.1.1 T1 해시 없음" in reports[-1].note


def test_enrichment_only_for_the_first_max_tries(monkeypatch):
    git = "".join(f"x\trefs/tags/9.{i}\n" for i in range(10))
    _e, _r, asked, _t = _phase2(monkeypatch, git=git, tag_db={}, max_tries=3)
    assert asked == ["9.9", "9.8", "9.7"]


def test_enrichment_stops_when_api_quota_runs_low(monkeypatch):
    git = "".join(f"x\trefs/tags/9.{i}\n" for i in range(6))
    _e, reports, asked, _t = _phase2(monkeypatch, git=git, tag_db={}, remaining=11)
    assert asked == ["9.5", "9.4"]                                       # 11 -> 10 -> 9 에서 멈춤
    assert "보강 멈춤" in reports[-1].note


def test_enrichment_stops_on_rate_limit(monkeypatch):
    git = "x\trefs/tags/9.1\nx\trefs/tags/9.0\n"
    _e, reports, asked, _t = _phase2(monkeypatch, git=git, tag_db=RS.ApiLimited("20:20"))
    assert asked == ["9.1"] and "20:20에 풀림" in reports[-1].note


def test_phase1_rate_limit_is_reported_and_no_tag_lookups(monkeypatch):
    tools = Tools(gh=False, ls_remote="x\trefs/tags/9.1\n")
    monkeypatch.setattr(RS, "which", tools.which)
    asked = []

    def limited():
        raise RS.ApiLimited("20:20")
    entries, reports = RS.collect(api=limited, api_tag=lambda t: asked.append(t),
                                  gyan_sha=lambda: "", run=tools.run, parallel=False)
    assert reports[0].note == "API 한도 초과 (20:20에 풀림)" and asked == [] and entries == []


def test_gh_view_only_for_top_candidates_missing_gh(monkeypatch):
    git = "".join(f"x\trefs/tags/9.{i}\n" for i in range(5))
    _e, _r, _asked, tools = _phase2(
        monkeypatch, git=git, gh=True, max_tries=2, tag_db={},
        gh_list=[{"tagName": f"9.{i}"} for i in range(5)], gh_assets={"9.4": A, "9.3": A})
    views = [c[3] for c in tools.calls if c[1:3] == ["release", "view"]]
    assert views == ["9.4", "9.3"]


def test_gh_rate_limit_is_named():
    p = types.SimpleNamespace(returncode=1, stderr="HTTP 403: API rate limit exceeded")
    assert RS._gh_why(p) == "gh 한도 초과"


# -- 저장된 목록 ------------------------------------------------------------------
from datetime import datetime, timedelta  # noqa: E402

NOW = datetime(2026, 11, 21, 21, 0)


def _cached(tmp_path, entries, *, days_old=1):
    book = _book(api=entries)
    RS.cross_check(book)
    path = tmp_path / "rel.json"
    RS.save_cache(path, book, now=NOW - timedelta(days=days_old))
    return path, RS.load_cache(path)


def test_cache_round_trip_and_broken_file(tmp_path):
    path, cache = _cached(tmp_path, [_api("9.1", A)])
    e = cache.entries["9.1"]
    assert (e.sha256, e.verified_by, e.url, e.kind) == (A, ["api"], "https://dl/9.1", "stable")
    assert cache.sha_for_name(RS.asset_name("9.1")) == (A, "9.1")
    path.write_text("{깨짐", encoding="utf-8")
    assert RS.load_cache(path) is None


def _down(monkeypatch):
    tools = Tools(gh=False, git=False)
    monkeypatch.setattr(RS, "which", tools.which)

    def blocked():
        raise urllib.error.URLError("막힘")
    return tools, blocked


def test_all_sources_down_uses_a_fresh_saved_list(monkeypatch, tmp_path):
    _p, cache = _cached(tmp_path, [_api("9.1", A), _api("9.0", B)])
    tools, blocked = _down(monkeypatch)
    saved = []
    entries, reports = RS.collect(api=blocked, gyan_sha=lambda: "", run=tools.run, cache=cache,
                                  save=saved.append, now=NOW, parallel=False)
    assert [e.tag for e in entries] == ["9.1", "9.0"] and entries[0].sha256 == A
    assert reports[-1].name == "저장된 목록" and reports[-1].ok and "대신 씀" in reports[-1].note
    assert saved == []                                                   # 새로 읽은 게 없으니 저장 안 함


def test_saved_list_older_than_seven_days_is_not_used(monkeypatch, tmp_path):
    _p, cache = _cached(tmp_path, [_api("9.1", A)], days_old=8)
    tools, blocked = _down(monkeypatch)
    entries, reports = RS.collect(api=blocked, gyan_sha=lambda: "", run=tools.run, cache=cache,
                                  now=NOW, parallel=False)
    assert entries == [] and any(r.name == "저장된 목록" and not r.ok for r in reports)


def test_changed_release_file_is_refused_and_old_hash_is_kept(monkeypatch, tmp_path):
    path, cache = _cached(tmp_path, [_api("9.1", A), _api("9.0", A)])
    tools = Tools(gh=False, git=False)
    monkeypatch.setattr(RS, "which", tools.which)
    entries, reports = RS.collect(api=lambda: [_api("9.1", B), _api("9.0", A)],
                                  gyan_sha=lambda: "", run=tools.run, cache=cache,
                                  save=lambda b: RS.save_cache(path, b, cache, now=NOW),
                                  parallel=False)
    assert [e.tag for e in entries] == ["9.0"]                          # 바뀐 9.1 은 안 씀
    assert any("9.1" in r.note and "바뀜" in r.note for r in reports)
    assert RS.load_cache(path).entries["9.1"].sha256 == A               # 바뀐 해시로 덮어쓰지 않음


def test_save_keeps_a_known_hash_when_this_run_had_none(tmp_path):
    path, cache = _cached(tmp_path, [_api("9.1", A)])
    book = _book(git="x\trefs/tags/9.1\n")                              # 이번엔 git 만, 해시 없음
    RS.cross_check(book)
    RS.save_cache(path, book, cache, now=NOW)
    assert RS.load_cache(path).entries["9.1"].sha256 == A


def test_usb_zip_is_checked_against_the_saved_list_by_name(tmp_path):
    data = _zip(b"usb")
    good = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "tools"
    book = _book(api=[_api("9.1", good), _api("9.0", A)])
    RS.cross_check(book)
    RS.save_cache(dest / F.CACHE_NAME, book, now=NOW)
    usb = tmp_path / "usb"
    usb.mkdir()
    (usb / RS.asset_name("9.1")).write_bytes(data)
    (usb / RS.asset_name("9.0")).write_bytes(data)                       # 이름은 9.0 인데 내용이 다름
    (usb / "ffmpeg-custom.zip").write_bytes(data)
    logs = []
    assert F.install_from_zip(usb / RS.asset_name("9.0"), dest, run=_ffmpeg_ok,
                              say=logs.append) == ""
    assert any("저장된 목록의 9.0" in m for m in logs)
    assert F.install_from_zip(usb / RS.asset_name("9.1"), dest, run=_ffmpeg_ok,
                              say=logs.append)
    assert any("해시 확인 통과 (저장된 목록의 9.1)" in m for m in logs)
    assert F.install_from_zip(usb / "ffmpeg-custom.zip", dest, run=_ffmpeg_ok, say=logs.append)
    assert any("기능 확인으로만" in m for m in logs)


def test_second_run_offline_installs_from_the_saved_list(tmp_path, monkeypatch):
    data = _zip(b"cached-run")
    sha = hashlib.sha256(data).hexdigest()
    tools = Tools(gh=False, git=False)
    monkeypatch.setattr(RS, "which", tools.which)
    url = "https://dl/9.1"
    rel = _api("9.1", sha)
    rel["assets"][0]["size"] = len(data)                              # 실제 크기(크기 검사)
    online = {F.RELEASES_API: json.dumps([rel]).encode(), url: data}
    r1 = F.install(tmp_path, opener=_opener(online), run=_ffmpeg_ok, tool_run=tools.run,
                   say=lambda _m: None)
    assert r1.ok and (tmp_path / F.CACHE_NAME).exists()
    (tmp_path / "ffmpeg").rename(tmp_path / "gone")                     # 다시 깔아야 하는 상황

    offline = {F.RELEASES_API: urllib.error.URLError("막힘"), url: data}
    r2 = F.install(tmp_path, opener=_opener(offline), run=_ffmpeg_ok, tool_run=tools.run,
                   say=lambda _m: None, force=True)
    assert r2.ok and r2.used.version == "9.1" and r2.used.sha256 == sha
    assert any(s.startswith("저장된 목록 됨") for s in r2.sources)


def test_gh_account_present_but_check_failed_is_not_called_logged_out(monkeypatch):
    def run(cmd, timeout=60):
        return types.SimpleNamespace(returncode=1, stderr="", stdout=(
            "github.com\n  X Failed to log in to github.com account me (keyring)\n"
            "  - Active account: true\n  - The token in keyring is invalid.\n"))
    monkeypatch.setattr(RS, "which", lambda n: n)
    exe, why = RS.gh_ready(run)
    assert exe == "" and "망이 막혔거나 토큰 만료" in why
    none = lambda cmd, timeout=60: types.SimpleNamespace(  # noqa: E731
        returncode=1, stderr="You are not logged into any GitHub hosts.", stdout="")
    assert RS.gh_ready(none) == ("", "gh 로그인 안 됨")
