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
정렬
  해시 있는 정식 버전 -> 해시 있는 개발 빌드 -> (allow_unverified 일 때만) 해시 없는 것

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


def read_gh(book_tags: Callable[[list[dict]], list[str]], *, run: Callable = _run,
            limit: int = 30, view_max: int = 8) -> tuple[list[dict], dict[str, list[dict]], str]:
    """(list 결과, 태그별 assets, 실패 이유)."""
    exe, why = gh_ready(run)
    if not exe:
        return [], {}, why
    p = run([exe, "release", "list", "-R", REPO, "--limit", str(limit), "--json",
             "tagName,isDraft,isPrerelease,publishedAt"])
    if p.returncode != 0:
        return [], {}, f"gh release list 실패: {(p.stderr or '').strip()[:120]}"
    items = json.loads(p.stdout or "[]")
    assets: dict[str, list[dict]] = {}
    for tag in book_tags(items)[:view_max]:          # view 는 시도할 앞쪽 몇 개만
        v = run([exe, "release", "view", tag, "-R", REPO, "--json", "assets"])
        if v.returncode == 0:
            assets[tag] = (json.loads(v.stdout or "{}") or {}).get("assets") or []
    return items, assets, ""


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


def _gh_view_order(items: list[dict]) -> list[str]:
    tmp: dict[str, Entry] = {}
    add_gh_list(tmp, items)
    return [e.tag for e in ordered_all(tmp)]


def ordered_all(book: dict[str, Entry]) -> list[Entry]:
    live = [e for e in book.values() if not e.draft]
    return sorted(live, key=lambda e: (e.kind == "stable", e.key), reverse=True)


def collect(*, api: Callable[[], list[dict]], gyan_sha: Callable[[], str],
            run: Callable = _run, allow_unverified: bool = False,
            parallel: bool = True) -> tuple[list[Entry], list[SourceReport]]:
    """세 출처를 함께 읽어 시도할 순서와 출처별 결과를 돌려줍니다."""
    def do_api():
        try:
            return api(), ""
        except Exception as exc:  # noqa: BLE001
            return None, f"API 실패: {exc}"

    def do_gh():
        try:
            return read_gh(_gh_view_order, run=run)
        except Exception as exc:  # noqa: BLE001
            return [], {}, f"gh 실패: {exc}"

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
    rels, why = api_res
    if rels is None:
        reports.append(SourceReport("API", False, why))
    else:
        n = add_api(book, rels)
        reports.append(SourceReport("API", True, f"릴리스 {len(rels)}개 (essentials {n}개)"))
    items, gh_assets, why = gh_res
    if why:
        reports.append(SourceReport("gh", False, why))
    else:
        add_gh_list(book, items)
        for tag, assets in gh_assets.items():
            add_gh_assets(book, tag, assets)
        reports.append(SourceReport("gh", True, f"릴리스 {len(items)}개, 해시 {len(gh_assets)}개 확인"))
    text, why = git_res
    if why:
        reports.append(SourceReport("git", False, why))
    else:
        n = add_git(book, text)
        reports.append(SourceReport("git", True,
                                    f"태그 {len(text.splitlines())}개 (릴리스 버전 {n}개)"))
    cross_check(book, gyan_sha() if any(not e.sha256 for e in book.values()) else "")
    conflicts = [e for e in book.values() if e.conflict]
    if conflicts:
        reports.append(SourceReport("교차 검증", False, ", ".join(
            f"{e.tag}: {e.conflict}" for e in conflicts)))
    return ordered(book, allow_unverified=allow_unverified), reports
