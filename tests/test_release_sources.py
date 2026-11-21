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
    assert [(r.name, r.ok) for r in reports] == [("API", True), ("gh", True), ("git", True)]
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
    assert [(r.name, r.ok) for r in reports] == [("API", False), ("gh", True), ("git", False)]


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
