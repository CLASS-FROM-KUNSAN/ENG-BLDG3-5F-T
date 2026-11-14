"""홈 정리: GitHub 에 고정된 사슬로 확인된 파일만 로컬에서 지웁니다.

**사용자 파일을 지우는 쪽입니다.** 그래서 의심스러우면 지우지 않습니다.

  1. 로컬 상태의 head 에서 사슬을 거슬러 올라가며 GitHub 에 올라간
     매니페스트를 하나씩 내려받아 해시를 확인합니다. 하나라도 틀리면
     (ChainError) rollback 하고 **한 개도 지우지 않습니다.**
  2. 확인된 매니페스트의 항목을 "정리 대기"(pending) 에 넣고, 확인한
     해시와 함께 **지우기 전에 commit** 합니다. 지우다가 죽어도 확인한
     일은 남습니다. 이미 확인한 고리는 다시 받지 않으므로(stop_at),
     앞선 실행에서 확인한 항목도 pending 에 남아 있어야 정리됩니다.
  3. 항목마다: 그 묶음(zip)이 릴리스에 같은 크기로 있는지, 경로가 홈
     안인지, 보호 경로가 아닌지, 리parse 포인트를 거치지 않는지,
     충분히 오래됐는지, 지금 내용의 sha256 이 올라간 것과 같은지
     전부 확인한 뒤에만 unlink 합니다. 디렉터리는 지우지 않습니다.
     **unlink 가 먼저, pending 에서 빼는 것이 나중**입니다. 그 사이에
     죽으면 다음 실행이 파일이 없는 것을 보고 'gone' 으로 정리합니다.
"""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ..logging_setup import get as get_logger
from ..paths import Layout
from ..state import sha256_file
from .chain import ChainError, Link, entries_of, walk_chain
from .homestate import HomeState, PendingRow
from .scan import is_cloud_placeholder, is_reparse_point

log = get_logger("backup")

PRUNE_LOG = "home-prune.log"
# 이만큼 pending 에서 뺄 때마다 commit 합니다. 끊겨도 다시 해시하지 않게.
COMMIT_EVERY = 1000

# 매니페스트의 bundles 에 없는 묶음. 원격 확인이 반드시 실패합니다.
_NO_BUNDLE = -1


@dataclass
class PruneResult:
    chain_ok: bool = True
    deleted: int = 0
    bytes_freed: int = 0
    changed: int = 0
    gone: int = 0
    too_new: int = 0
    protected: int = 0
    missing_remote: int = 0
    errors: int = 0
    # dry-run 에서 지웠을 개수. 실제로 지운 것은 deleted 입니다.
    would_delete: int = 0
    paths: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 경로 안전
# ---------------------------------------------------------------------------
def _is_reparse(path: Path) -> bool:
    """정션/심볼릭 링크면 True. 따라가지 않고(lstat) 봅니다.

    테스트가 바꿔 끼울 수 있도록 함수 하나로 둡니다.
    """
    st = path.lstat()
    return stat.S_ISLNK(st.st_mode) or is_reparse_point(st)


def _unlink(path: Path) -> None:
    """테스트가 '지우다 죽는' 상황을 흉내 낼 수 있게 한 곳에 둡니다."""
    path.unlink()


def safe_rel(rel: Any) -> PurePosixPath | None:
    """매니페스트의 path 가 홈 기준의 얌전한 상대 경로인지. 아니면 None.

    절대 경로, 드라이브 문자, 역슬래시, ':'(드라이브·대체 데이터
    스트림), '..' / '.' / 빈 칸은 전부 거부합니다.
    """
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or "\x00" in rel:
        return None
    p = PurePosixPath(rel)
    if p.is_absolute() or rel.startswith("/"):
        return None
    w = PureWindowsPath(rel)
    if w.drive or w.root or w.anchor:
        return None
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return p


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _fold(rel: str) -> str:
    """Win32 이름 비교용: NFC + 대소문자 무시."""
    return unicodedata.normalize("NFC", rel).casefold()


def _same_home(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _inside(child: Path, parent: Path) -> bool:
    """resolve 된 두 경로로, child 가 parent 와 같거나 그 아래인지."""
    c, a = _norm(child), _norm(parent).rstrip("\\/")
    return c == a or c.startswith(a + os.sep)


def _protected(target: Path, protect: list[Path]) -> bool:
    return any(_inside(target, p) for p in protect)


def _passes_reparse(home: Path, rel: PurePosixPath) -> bool:
    """home 과 파일 사이의 모든 디렉터리와 파일 자신이 리parse 포인트가 아닌지."""
    cur = home
    for part in rel.parts:
        cur = cur / part
        if _is_reparse(cur):
            return False
    return True


# ---------------------------------------------------------------------------
# 사슬 확인
# ---------------------------------------------------------------------------
def _make_fetch(uploader, fetched: list[str]):
    def fetch(link: Link) -> bytes:
        try:
            asset = uploader.find_asset(link.tag, link.asset)
            if asset is None:
                raise ChainError(f"GitHub 에 매니페스트가 없습니다: {link.tag}/{link.asset}")
            data = uploader.download_asset(asset)
        except ChainError:
            raise
        except Exception as exc:
            # 네트워크가 끊겨도 '확인 실패' 입니다. 확인 못 한 것은 지우지 않습니다.
            raise ChainError(
                f"매니페스트를 받지 못했습니다: {link.tag}/{link.asset} ({exc})") from exc
        fetched.append(link.sha256)
        return data
    return fetch


def _bundle_map(manifest: dict[str, Any]) -> dict[str, tuple[int, int, str]]:
    """묶음 이름 -> (size, parts, sha256). 형식이 이상한 묶음은 빠집니다."""
    out: dict[str, tuple[int, int, str]] = {}
    for b in manifest.get("bundles") or []:
        if not isinstance(b, dict):
            continue
        name, size = b.get("asset"), b.get("size")
        parts = b.get("parts", 0) or 0
        if not isinstance(name, str) or not isinstance(size, int) or not isinstance(parts, int):
            continue
        sha = b.get("sha256") if isinstance(b.get("sha256"), str) else ""
        out[name] = (size, parts, sha)
    return out


def _pending_rows(m: dict[str, Any]):
    """매니페스트 하나의 항목을 PendingRow 로. 같은 경로는 state 가 seq 로 고릅니다."""
    bundles = _bundle_map(m)
    created = m.get("created_at") if isinstance(m.get("created_at"), str) else ""
    for e in entries_of(m):
        size, parts, bsha = bundles.get(e.bundle, (_NO_BUNDLE, 0, ""))
        yield PendingRow(
            path=e.path, size=e.size, mtime_ns=e.mtime_ns, sha256=e.sha256,
            bundle=e.bundle, member=e.member, tag=m["tag"], seq=m["seq"],
            created_at=created, bundle_size=size, bundle_parts=parts, bundle_sha256=bsha)


def _verify(state: HomeState, uploader, machine: str,
            home: Path) -> tuple[Link, int, int] | None:
    """사슬을 확인하고 새 항목을 pending 에 넣습니다(commit 은 하지 않음).

    반환: (head, head seq, 새 고리 수). head 가 없으면 None. 실패는 ChainError.
    """
    got = state.get_head()
    if got is None:
        return None
    head, head_seq = got
    fetched: list[str] = []
    count = 0
    home_s = str(Path(home).resolve())

    def visit(m: dict[str, Any]) -> None:
        # 고리를 하나씩 받아 바로 넣고 놓아 줍니다(매니페스트 하나에 수만 항목).
        # 같은 경로는 seq 가 큰 쪽이 이기므로 최신→과거 순서여도 맞습니다.
        # 사슬이 중간에 끊기면 호출자가 rollback 해서 여기서 넣은 것도 사라집니다.
        nonlocal count
        m_home = m.get("home")
        if not isinstance(m_home, str) or not _same_home(m_home, home_s):
            raise ChainError(
                f"다른 홈 디렉터리의 사슬입니다: {m_home!r} != {home_s!r} (seq {m['seq']})")
        if count == 0 and m["seq"] != head_seq:
            raise ChainError(
                f"head 의 seq {head_seq!r} 와 매니페스트 seq {m['seq']!r} 가 다릅니다")
        state.upsert_pending(_pending_rows(m))
        count += 1

    walk_chain(head, _make_fetch(uploader, fetched), machine=machine,
               stop_at=state.verified(), visit=visit)
    state.add_verified(fetched)
    return head, head_seq, count


# ---------------------------------------------------------------------------
# 원격 묶음 확인
# ---------------------------------------------------------------------------
class _Remote:
    """묶음이 GitHub 에 **올라간 그 바이트로** 있는지. 확신이 없으면 False.

    크기만 맞는지 보면, 확인한 뒤 같은 이름으로 다른 묶음이 올라온 경우를
    못 잡습니다(실제로 유일한 사본을 지운 적이 있습니다). 그래서 GitHub 가
    주는 sha256 digest 가 반드시 있어야 하고 매니페스트와 같아야 합니다.
    분할 묶음은 parts.json 을 내려받아 원본 sha256 과 조각 구성을 맞춥니다.
    태그마다 이름 -> (크기, digest) 만, 묶음마다 결과(bool)만 둡니다.
    """

    def __init__(self, uploader) -> None:
        self.uploader = uploader
        self.cache: dict[str, dict[str, tuple[Any, Any]] | None] = {}
        self.verdicts: dict[tuple[str, str], bool] = {}
        self.failed_tags: set[str] = set()

    def _assets(self, tag: str) -> dict[str, tuple[Any, Any]] | None:
        if tag not in self.cache:
            try:
                rel = self.uploader.get_release(tag)
                assets = self.uploader.list_assets(rel) if rel else []
                self.cache[tag] = {a.get("name"): (a.get("size"), a.get("digest"))
                                   for a in assets if isinstance(a, dict)}
            except Exception as exc:
                log.error("릴리스 %s 의 에셋 목록을 받지 못했습니다: %s", tag, exc)
                self.cache[tag] = None
                self.failed_tags.add(tag)
        return self.cache[tag]

    @staticmethod
    def _has_digest(a: tuple[Any, Any] | None) -> bool:
        return (a is not None and isinstance(a[1], str) and a[1].startswith("sha256:")
                and len(a[1]) == len("sha256:") + 64)

    def _parts_json(self, tag: str, name: str) -> dict[str, Any] | None:
        try:
            asset = self.uploader.find_asset(tag, f"{name}.parts.json")
            if asset is None:
                return None
            data = json.loads(self.uploader.download_asset(asset).decode("utf-8"))
        except Exception as exc:
            log.warning("분할 정보를 읽지 못했습니다: %s/%s.parts.json (%s)", tag, name, exc)
            return None
        return data if isinstance(data, dict) else None

    def _check(self, row: PendingRow) -> bool:
        if row.bundle_size == _NO_BUNDLE or len(row.bundle_sha256) != 64:
            return False
        assets = self._assets(row.tag)
        if not assets:
            return False
        name, size, parts = row.bundle, row.bundle_size, row.bundle_parts
        if parts <= 0:
            a = assets.get(name)
            return (self._has_digest(a) and a[0] == size
                    and a[1] == f"sha256:{row.bundle_sha256}")
        info = self._parts_json(row.tag, name)
        if info is None:
            return False
        if (info.get("sha256") != row.bundle_sha256 or info.get("part_count") != parts
                or info.get("part_prefix") != name or info.get("original_size") != size):
            return False
        total = 0
        for i in range(1, parts + 1):
            a = assets.get(f"{name}.part{i:03d}")
            if not self._has_digest(a) or not isinstance(a[0], int):
                return False
            total += a[0]
        return total == size

    def ok(self, row: PendingRow) -> bool:
        key = (row.tag, row.bundle)
        if key not in self.verdicts:
            self.verdicts[key] = self._check(row)
            if not self.verdicts[key]:
                log.warning("GitHub 에서 묶음을 확인하지 못했습니다(지우지 않음): %s/%s",
                            row.tag, row.bundle)
        return self.verdicts[key]


# ---------------------------------------------------------------------------
# 본체
# ---------------------------------------------------------------------------
def _parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _group(paths: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for p in sorted(paths):
        pp = PurePosixPath(p)
        d = str(pp.parent)
        groups.setdefault("(홈)" if d == "." else d, []).append(pp.name)
    return groups


def _write_prune_log(layout: Layout, *, now: datetime, head: Link, seq: int,
                     res: PruneResult) -> None:
    path = layout.logs / PRUNE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"== {now.isoformat(timespec='seconds')}  head seq {seq}  {head.tag}/{head.asset}"
        f"  지움 {res.deleted}개 {res.bytes_freed}바이트"
        f"  (바뀜 {res.changed}, 없음 {res.gone}, 최근 {res.too_new}, 보호 {res.protected},"
        f" 원격없음 {res.missing_remote}, 오류 {res.errors}) =="
    ]
    for d, names in _group(res.paths).items():
        lines.append(f"{d}/")
        lines.extend(f"  {n}" for n in names)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _judge(row: PendingRow, *, home_r: Path, guard: list[Path], remote: _Remote,
           now: datetime, min_age: timedelta,
           res: PruneResult) -> tuple[str, Path | None, str]:
    """항목 하나를 판정합니다.

    ('delete', 실제경로, 홈 기준 실제 상대경로) | ('drop', None, '') | ('keep', None, '').
    """
    rel = safe_rel(row.path)
    if rel is None or str(rel) != row.path:
        log.error("이상한 경로라 지우지 않고 목록에서 뺍니다: %r", row.path)
        res.errors += 1
        return "drop", None, ""

    if not remote.ok(row):
        res.missing_remote += 1
        return "keep", None, ""

    created = _parse_time(row.created_at)
    if created is None or now - created < min_age:
        res.too_new += 1
        return "keep", None, ""      # 시간이 지나면 지울 수 있게 됩니다

    target = home_r.joinpath(*rel.parts)
    try:
        if not _passes_reparse(home_r, rel):
            log.warning("리parse 포인트를 거치는 경로라 건너뜁니다: %s", row.path)
            res.protected += 1
            return "drop", None, ""
        st = target.lstat()
        if not stat.S_ISREG(st.st_mode) or is_cloud_placeholder(st):
            res.protected += 1
            return "drop", None, ""
        real = target.resolve(strict=True)
        if not _inside(real, home_r) or real == home_r:
            log.error("홈 밖을 가리켜 지우지 않습니다: %s -> %s", row.path, real)
            res.errors += 1
            return "drop", None, ""
        # Win32 별칭: 'a.' / 'a ' / 8.3 짧은 이름은 다른 이름의 파일을 엽니다.
        # 실제 이름이 항목의 경로와 같아야만 그 파일입니다.
        real_rel = real.relative_to(home_r).as_posix()
        if _fold(real_rel) != _fold(row.path):
            log.error("항목 경로와 실제 파일 이름이 달라 지우지 않습니다: %s -> %s",
                      row.path, real_rel)
            res.errors += 1
            return "drop", None, ""
        if _protected(real, guard):
            res.protected += 1
            return "drop", None, ""
        if st.st_size != row.size or sha256_file(real) != row.sha256:
            res.changed += 1         # 새 내용은 다음 백업이 올립니다
            return "drop", None, ""
    except FileNotFoundError:
        res.gone += 1
        return "drop", None, ""
    except OSError as exc:
        log.warning("확인하지 못했습니다: %s (%s)", row.path, exc)
        res.errors += 1
        return "keep", None, ""
    return "delete", real, real_rel


def run_prune(*, home: Path, uploader, state: HomeState, layout: Layout, machine: str,
              min_age_hours: float, protect: list[Path], dry_run: bool,
              now: datetime | None = None) -> PruneResult:
    """사슬을 확인하고, 확인된 항목 중 조건을 다 만족하는 파일만 지웁니다.

    state 의 트랜잭션: 사슬 확인 실패면 rollback, 성공이면 지우기 전에 commit,
    지우는 동안 COMMIT_EVERY 건마다 commit. dry-run 은 끝에 rollback 하고
    commit 하지 않습니다.
    """
    res = PruneResult()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    tag_dry = "[dry-run] " if dry_run else ""

    # --- 1) 사슬 확인: 실패하면 아무것도 지우지 않습니다 ---
    try:
        verified = _verify(state, uploader, machine, home)
    except ChainError as exc:
        state.rollback()
        log.error("홈 백업 사슬 확인 실패 — 아무것도 지우지 않습니다: %s", exc)
        res.chain_ok = False
        return res
    if verified is None:
        state.rollback()
        log.info("%s아직 사슬이 없습니다. 정리할 것이 없습니다.", tag_dry)
        return res
    head, head_seq, new_links = verified
    if not dry_run:
        state.commit()           # 확인한 일은 지우기 전에 남깁니다
    log.info("%s사슬 확인: 새 고리 %d개, 정리 대기 %d개",
             tag_dry, new_links, state.pending_count())

    # --- 2) 항목마다 확인 후 정리 ---
    home_r = Path(home).resolve(strict=True)
    guard = [Path(p).resolve() for p in protect] + [Path(layout.root).resolve()]
    remote = _Remote(uploader)
    min_age = timedelta(hours=float(min_age_hours))
    removed = 0

    try:
        for row in state.iter_pending():
            verdict, real, real_rel = _judge(row, home_r=home_r, guard=guard, remote=remote,
                                   now=now, min_age=min_age, res=res)
            if verdict == "keep":
                continue
            if verdict == "delete":
                if dry_run:
                    res.would_delete += 1
                    res.paths.append(real_rel)
                    continue
                try:
                    _unlink(real)
                except FileNotFoundError:
                    res.gone += 1
                except OSError as exc:
                    log.warning("지우지 못했습니다: %s (%s)", row.path, exc)
                    res.errors += 1
                    continue
                else:
                    res.deleted += 1
                    res.bytes_freed += row.size
                    res.paths.append(real_rel)      # 실제로 지운 이름
                    log.debug("지움: %s", real)
            if dry_run:
                continue
            # 지운 뒤에 뺍니다. 그 사이에 죽어도 다음 번엔 'gone' 입니다.
            state.remove_pending([row.path])
            removed += 1
            if removed % COMMIT_EVERY == 0:
                state.commit()
    except BaseException:
        # 중간에 죽어도 이미 지운 파일은 기록에 남깁니다. commit 안 된 pending
        # 은 다음 실행이 'gone' 으로 정리합니다.
        if not dry_run and res.paths:
            try:
                _write_prune_log(layout, now=now, head=head, seq=head_seq, res=res)
            except OSError:
                pass
        raise
    finally:
        if dry_run:
            state.rollback()

    if remote.failed_tags:
        res.errors += len(remote.failed_tags)

    # --- 3) 기록 ---
    for d, names in _group(res.paths).items():
        if dry_run:
            log.info("[dry-run] 지울 파일 %s/ (%d개): %s", d, len(names), ", ".join(names))
        else:
            log.info("지웠습니다 %s/ — %d개", d, len(names))
    log.info(
        "%s정리 결과: 지움 %d개(%d바이트), 지울 예정 %d개, 바뀜 %d, 없음 %d, 최근 %d, "
        "보호 %d, 원격 없음 %d, 오류 %d",
        tag_dry, res.deleted, res.bytes_freed, res.would_delete, res.changed, res.gone,
        res.too_new, res.protected, res.missing_remote, res.errors)

    if dry_run:
        return res

    state.commit()
    _write_prune_log(layout, now=now, head=head, seq=head_seq, res=res)
    return res
