"""ffmpeg 릴리스 목록을 세 출처(GitHub API · gh · git)에서 함께 읽어 하나로 합칩니다.

    [목록]  API ─┐
            gh  ─┼─▶ 정규화 ─▶ 태그별 합치기 ─▶ 교차 검증 ─▶ 신뢰도순 정렬
            git ─┘   (동시에, 하나가 실패해도 나머지로)

출처마다 아는 것이 다릅니다(2026-11 실제 조회).

  API  목록·첨부 파일·sha256·날짜. 로그인 없이 시간당 60회
  gh   list 는 태그·날짜만, view <태그> 에 첨부 파일·sha256. 설치·로그인 필요
  git  ls-remote --tags 로 태그 이름만(릴리스가 아닌 tools-* 태그도 섞임).
       첨부 파일은 못 받지만 주소는 태그로 만들 수 있음:
       releases/download/<태그>/ffmpeg-<태그>-essentials_build.zip

정규화
  태그   refs/tags/·끝의 ^{}·앞의 v 를 뗌. 9.1 같은 정식 버전(숫자 순)과
         2026-11-19-git-… 개발 빌드(태그 속 날짜 순)만 남기고 나머지는 버림
  해시   'sha256:HEX', 'HEX *파일명', 'HEX' -> 소문자 64자리
교차 검증
  API 와 gh 의 sha256 이 다르면 그 버전은 버림(변조·불일치)
  같으면 '두 출처 확인'
  git 에만 있으면 주소를 만들고(만든 주소 표시) 해시가 없으므로 뒤로
  gyan.dev 의 .sha256 은 최신 정식 버전과 같은 빌드라, 해시 없는 최신 정식
  버전에 빌려 붙임(API·gh 가 막혀도 git + gyan.dev 로 검증 가능)
신뢰 등급(쓸지 말지만 정함 — 순서는 종류 -> 버전)
  T3 두 출처의 해시가 같음   T2 한 출처의 해시   T1 해시 없음   T0 출처끼리 다름(버림)
  T1 은 allow_unverified 일 때만. 2025년 이전에 올라간 파일은 GitHub 에 해시가 없어
  어느 출처로도 T2 가 될 수 없습니다(실제 6.1.1 확인).
두 번 읽기
  1단계(넓게)  API 목록 1쪽(100개) · gh release list · git ls-remote 를 동시에
  2단계(보강)  시도할 앞쪽 후보 중 해시가 없거나 한 출처뿐인 것만 다시:
               gh 가 로그인돼 있으면 gh release view(gh 확인 추가),
               해시가 아예 없으면 API 로 태그 하나 조회 — API 남은 한도가 10 미만이면 멈춤
한도·막힘
  API 403/429 + 남은 한도 0 -> 한도 초과(다시 안 함, 풀리는 시각 표시)
  API 403 + 한도 남음       -> 막힘(학교망 등)
  gh 출력에 rate limit      -> gh 한도 초과

gh 가 없거나 로그인이 안 돼 있으면 그 출처만 건너뜁니다. 로그인 창을 띄우거나
토큰을 찾지 않습니다(GH_PROMPT_DISABLED, GIT_TERMINAL_PROMPT=0).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

REPO = "GyanD/codexffmpeg"
GIT_URL = f"https://github.com/{REPO}.git"
ASSET_SUFFIX = "-essentials_build.zip"
which = shutil.which              # 테스트에서 바꿔 끼움(진짜 gh·git 을 부르지 않게)

_STABLE = re.compile(r"\d+(\.\d+)+")
_NIGHTLY = re.compile(r"(\d{4})-(\d{2})-(\d{2})-git-[0-9a-f]+")
_HEX = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------------------
# 정규화
# ---------------------------------------------------------------------------
def normalize_tag(raw: str) -> str:
    t = str(raw or "").strip()
    t = t.removeprefix("refs/tags/").removesuffix("^{}")
    if len(t) > 1 and t[0] in "vV" and t[1].isdigit():
        t = t[1:]
    return t


def classify(tag: str) -> tuple[str, tuple] | None:
    """('stable', (9, 1)) · ('nightly', (2026, 11, 19)) · 그 밖은 None(버림)."""
    if _STABLE.fullmatch(tag):
        return "stable", tuple(int(x) for x in tag.split("."))
    m = _NIGHTLY.fullmatch(tag)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            date(y, mo, d)
        except ValueError:
            return None
        return "nightly", (y, mo, d)
    return None


def norm_sha(value: str) -> str:
    s = str(value or "").strip().lower()
    s = s.removeprefix("sha256:")
    s = s.split()[0] if s else ""
    return s if _HEX.fullmatch(s) else ""


def asset_name(tag: str) -> str:
    return f"ffmpeg-{tag}{ASSET_SUFFIX}"


def asset_url(tag: str) -> str:
    return f"https://github.com/{REPO}/releases/download/{tag}/{asset_name(tag)}"


# ---------------------------------------------------------------------------
# 합친 모양
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    tag: str
    kind: str                         # stable / nightly
    key: tuple
    url: str = ""
    size: int = 0
    published: str = ""
    sha: dict[str, str] = field(default_factory=dict)      # 출처 -> sha256
    sources: set[str] = field(default_factory=set)
    synthesized: bool = False         # git 에서 와서 주소를 만들었음
    draft: bool = False
    conflict: str = ""                # 비어 있지 않으면 버림

    @property
    def sha256(self) -> str:
        vals = {v for v in self.sha.values() if v}
        return next(iter(vals)) if len(vals) == 1 else ""

    @property
    def verified_by(self) -> list[str]:
        return sorted(k for k, v in self.sha.items() if v and v == self.sha256)

    @property
    def trust(self) -> int:
        """3 두 출처 일치 · 2 한 출처 · 1 해시 없음 · 0 출처끼리 다름."""
        if self.conflict:
            return 0
        return min(3, 1 + len(self.verified_by)) if self.sha256 else 1


@dataclass
class ApiResult:
    """API 응답과 남은 한도. remaining 이 None 이면 모름(헤더 없음)."""
    data: object
    remaining: int | None = None
    limit: int | None = None


class ApiLimited(Exception):
    """API 한도 초과 — 다시 해도 소용없음."""

    def __init__(self, reset: str = "") -> None:
        super().__init__(f"API 한도 초과{f' ({reset}에 풀림)' if reset else ''}")
        self.reset = reset


class ApiBlocked(Exception):
    """403 인데 한도는 남음 — 학교망 등이 막음."""


def api_error(code: int, headers) -> Exception | None:
    """HTTP 오류를 한도 초과/막힘으로 가립니다(순수 함수, 시험용)."""
    if code not in (403, 429):
        return None
    get = (headers or {}).get
    remaining = str(get("X-RateLimit-Remaining") or "")
    if remaining == "0" or code == 429:
        reset = ""
        raw = str(get("X-RateLimit-Reset") or "")
        if raw.isdigit():
            import datetime as _dt
            reset = _dt.datetime.fromtimestamp(int(raw)).strftime("%H:%M")
        return ApiLimited(reset)
    return ApiBlocked(f"API 가 막혀 있음 (HTTP {code})")


@dataclass
class SourceReport:
    name: str
    ok: bool
    note: str


def _entry(book: dict[str, Entry], raw_tag: str, source: str) -> Entry | None:
    tag = normalize_tag(raw_tag)
    c = classify(tag)
    if c is None:
        return None
    e = book.get(tag)
    if e is None:
        e = book[tag] = Entry(tag, c[0], c[1])
    e.sources.add(source)
    return e


def add_api(book: dict[str, Entry], releases: list[dict]) -> int:
    n = 0
    for rel in releases or []:
        e = _entry(book, rel.get("tag_name", ""), "api")
        if e is None:
            continue
        e.draft = e.draft or bool(rel.get("draft"))
        e.published = e.published or str(rel.get("published_at") or "")
        for a in rel.get("assets") or []:
            if str(a.get("name", "")) == asset_name(e.tag):
                e.url = str(a.get("browser_download_url") or e.url)
                e.size = int(a.get("size") or e.size)
                e.sha["api"] = norm_sha(a.get("digest", ""))
                n += 1
    return n


def add_gh_list(book: dict[str, Entry], items: list[dict]) -> None:
    for it in items or []:
        e = _entry(book, it.get("tagName", ""), "gh")
        if e is not None:
            e.draft = e.draft or bool(it.get("isDraft"))
            e.published = e.published or str(it.get("publishedAt") or "")


def add_gh_assets(book: dict[str, Entry], tag: str, assets: list[dict]) -> None:
    e = book.get(normalize_tag(tag))
    if e is None:
        return
    for a in assets or []:
        if str(a.get("name", "")) == asset_name(e.tag):
            e.url = e.url or str(a.get("url") or "")
            e.size = e.size or int(a.get("size") or 0)
            e.sha["gh"] = norm_sha(a.get("digest", ""))


def add_git(book: dict[str, Entry], ls_remote: str) -> int:
    n = 0
    for line in (ls_remote or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and _entry(book, parts[1], "git") is not None:
            n += 1
    return n


def cross_check(book: dict[str, Entry], gyan_sha: str = "") -> None:
    """출처끼리 맞춰 보고, 만든 주소·빌린 해시를 채웁니다."""
    for e in book.values():
        api, gh = e.sha.get("api", ""), e.sha.get("gh", "")
        if api and gh and api != gh:
            e.conflict = "API 와 gh 의 SHA-256 이 다름"
        if not e.url:
            e.url, e.synthesized = asset_url(e.tag), True
    stable = [e for e in book.values() if e.kind == "stable" and not e.draft and not e.conflict]
    if gyan_sha and stable:
        newest = max(stable, key=lambda e: e.key)
        if not e_has_sha(newest):
            newest.sha["gyan.dev"] = gyan_sha


def e_has_sha(e: Entry) -> bool:
    return bool(e.sha256)


def ordered(book: dict[str, Entry], *, allow_unverified: bool = False) -> list[Entry]:
    live = [e for e in book.values() if not e.draft and not e.conflict]
    v_stable = sorted((e for e in live if e.kind == "stable" and e.sha256), key=lambda e: e.key,
                      reverse=True)
    v_night = sorted((e for e in live if e.kind == "nightly" and e.sha256), key=lambda e: e.key,
                     reverse=True)
    rest = []
    if allow_unverified:
        rest = sorted((e for e in live if not e.sha256),
                      key=lambda e: (e.kind == "stable", e.key), reverse=True)
    return v_stable + v_night + rest


# ---------------------------------------------------------------------------
# 출처에서 읽기 (동시에)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env, creationflags=flags)


def gh_ready(run: Callable = _run) -> tuple[str, str]:
    """(gh 경로, '') 또는 ('', 안 되는 이유). 로그인을 시키지 않습니다."""
    exe = which("gh")
    if not exe:
        return "", "gh 없음"
    try:
        if run([exe, "auth", "status"], timeout=20).returncode != 0:
            return "", "gh 로그인 안 됨"
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"gh 실행 실패: {exc}"
    return exe, ""


def _gh_why(p) -> str:
    err = (p.stderr or "").strip()
    return "gh 한도 초과" if "rate limit" in err.lower() else err[:120]


def read_gh(*, run: Callable = _run, limit: int = 100) -> tuple[str, list[dict], str]:
    """1단계: (gh 경로, release list 결과, 실패 이유)."""
    exe, why = gh_ready(run)
    if not exe:
        return "", [], why
    p = run([exe, "release", "list", "-R", REPO, "--limit", str(limit), "--json",
             "tagName,isDraft,isPrerelease,publishedAt"])
    if p.returncode != 0:
        return "", [], f"gh release list 실패: {_gh_why(p)}"
    return exe, json.loads(p.stdout or "[]"), ""


def gh_view(exe: str, tag: str, *, run: Callable = _run) -> list[dict] | None:
    p = run([exe, "release", "view", tag, "-R", REPO, "--json", "assets"], timeout=30)
    if p.returncode != 0:
        return None
    return (json.loads(p.stdout or "{}") or {}).get("assets") or []


def read_git(*, run: Callable = _run) -> tuple[str, str]:
    exe = which("git")
    if not exe:
        return "", "git 없음"
    try:
        p = run([exe, "ls-remote", "--tags", GIT_URL], timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"git 실행 실패: {exc}"
    if p.returncode != 0:
        return "", f"git ls-remote 실패: {(p.stderr or '').strip()[:120]}"
    return p.stdout or "", ""


def ordered_all(book: dict[str, Entry]) -> list[Entry]:
    live = [e for e in book.values() if not e.draft]
    return sorted(live, key=lambda e: (e.kind == "stable", e.key), reverse=True)


MIN_API_LEFT = 10           # 이보다 적게 남으면 2단계에서 API 를 더 부르지 않음


def _as_api(res) -> ApiResult:
    return res if isinstance(res, ApiResult) else ApiResult(res)


def collect(*, api: Callable[[], object], gyan_sha: Callable[[], str],
            api_tag: Callable[[str], object] | None = None,
            run: Callable = _run, allow_unverified: bool = False, max_tries: int = 6,
            parallel: bool = True) -> tuple[list[Entry], list[SourceReport]]:
    """세 출처를 함께 읽고(1단계) 앞쪽 후보만 보강해(2단계) 시도할 순서를 돌려줍니다.

    api      목록 1쪽 -> list 또는 ApiResult(남은 한도 포함)
    api_tag  태그 하나 -> release dict 또는 ApiResult. 없으면 API 보강 안 함
    """
    def do_api():
        try:
            return _as_api(api()), ""
        except ApiLimited as exc:
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001
            return None, f"API 실패: {exc}"

    def do_gh():
        try:
            return read_gh(run=run)
        except Exception as exc:  # noqa: BLE001
            return "", [], f"gh 실패: {exc}"

    def do_git():
        try:
            return read_git(run=run)
        except Exception as exc:  # noqa: BLE001
            return "", f"git 실패: {exc}"

    if parallel:
        with ThreadPoolExecutor(max_workers=3) as pool:
            fa, fg, fgit = pool.submit(do_api), pool.submit(do_gh), pool.submit(do_git)
            api_res, gh_res, git_res = fa.result(), fg.result(), fgit.result()
    else:
        api_res, gh_res, git_res = do_api(), do_gh(), do_git()

    book: dict[str, Entry] = {}
    reports = []
    res, why = api_res
    api_left: int | None = None
    api_ok = res is not None
    if res is None:
        reports.append(SourceReport("API", False, why))
    else:
        rels = res.data or []
        api_left = res.remaining
        n = add_api(book, rels)
        quota = f", 한도 {res.remaining}/{res.limit}" if res.remaining is not None else ""
        reports.append(SourceReport("API", True,
                                    f"릴리스 {len(rels)}개 (essentials {n}개{quota})"))
    gh_exe, items, why = gh_res
    if why:
        reports.append(SourceReport("gh", False, why))
    else:
        add_gh_list(book, items)
        reports.append(SourceReport("gh", True, f"릴리스 {len(items)}개"))
    text, why = git_res
    if why:
        reports.append(SourceReport("git", False, why))
    else:
        n = add_git(book, text)
        reports.append(SourceReport("git", True,
                                    f"태그 {len(text.splitlines())}개 (릴리스 버전 {n}개)"))
    cross_check(book)                                       # 주소 채우기 + 1차 불일치

    # -- 2단계: 시도할 앞쪽 후보만 보강 ------------------------------------------
    top = [e for e in ordered_all(book) if not e.conflict][:max_tries]
    notes: list[str] = []

    def enrich_gh(e: Entry) -> None:
        assets = gh_view(gh_exe, e.tag, run=run)
        if assets is not None:
            add_gh_assets(book, e.tag, assets)

    gh_todo = [e for e in top if gh_exe and "gh" not in e.sha]
    if gh_todo:
        if parallel:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(enrich_gh, gh_todo))
        else:
            for e in gh_todo:
                enrich_gh(e)
    for e in top:
        if e.sha256 or api_tag is None or not api_ok:
            continue
        if api_left is not None and api_left < MIN_API_LEFT:
            notes.append(f"API 남은 한도 {api_left} — 보강 멈춤")
            break
        try:
            got = _as_api(api_tag(e.tag))
        except ApiLimited as exc:
            notes.append(str(exc))
            api_ok = False
            break
        except Exception as exc:  # noqa: BLE001 — 그 태그만 못 봄
            notes.append(f"{e.tag} API 조회 실패: {exc}")
            continue
        if got.remaining is not None:
            api_left = got.remaining
        if isinstance(got.data, dict):
            add_api(book, [got.data])
    cross_check(book, gyan_sha() if any(not e.sha256 for e in top) else "")
    for e in top:
        if e.conflict:
            continue
        if e.sha256:
            notes.append(f"{e.tag} T{e.trust} {'·'.join(e.verified_by)}")
        else:
            notes.append(f"{e.tag} T1 해시 없음")
    if notes:
        reports.append(SourceReport("보강", True, ", ".join(notes)))
    conflicts = [e for e in book.values() if e.conflict]
    if conflicts:
        reports.append(SourceReport("교차 검증", False, ", ".join(
            f"{e.tag}: {e.conflict}" for e in conflicts)))
    return ordered(book, allow_unverified=allow_unverified), reports
