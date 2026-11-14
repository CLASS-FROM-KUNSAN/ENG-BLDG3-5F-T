"""홈 백업 한 번 돌리기: 스캔 → 증분 → 묶기 → 올리기 → 매니페스트(사슬 고리).

**매니페스트가 올라간 뒤에만** 로컬 상태를 바꿉니다. 묶음은 올라갔는데
매니페스트가 실패하면, 그 묶음은 사슬에 없으므로 정리가 근거로 쓰지
않습니다. 로컬 상태도 그대로라 다음 실행이 같은 파일을 다시 올립니다.

홈에는 파일이 66만 개쯤 있습니다. 그래서
  - 스캔과 묶기는 이터레이터로 흘려보냅니다(메모리에는 묶음 하나와
    지금 고리의 항목들만 있습니다).
  - 고리 하나에 항목을 MAX_LINK_ENTRIES 개까지만 넣고, 차면 그 고리의
    매니페스트를 올리고 commit 한 뒤 다음 고리로 넘어갑니다. 첫 실행이
    몇 시간 걸리다 중간에 죽어도 끝난 고리는 남습니다.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger
from ..paths import Layout
from ..release import ReleaseError, asset_name
from ..state import sha256_file
from .bundle import plan_bundles, write_bundle
from .chain import Entry, Link, build_manifest, canonical_bytes, digest
from .homestate import FileRow, HomeState
from .scan import iter_home

log = get_logger("backup")

CHAIN_LOG = "home-chain.log"
MAX_LINK_ENTRIES = 50_000
MAX_CONSECUTIVE_FAILURES = 3   # 연달아 이만큼 실패하면 네트워크/토큰 문제로 보고 멈춥니다
_LOG_FOLDERS = 20
_SHOW = 5


@dataclass
class BackupResult:
    seq: int | None                # 이번 실행에서 마지막으로 사슬에 붙은 seq(없으면 None)
    uploaded_files: int            # 올라간 묶음에 든 파일 수(사슬에 붙었는지와 무관)
    skipped_unchanged: int
    failed_bundles: int
    unreadable: int
    excluded: int
    bytes: int
    candidates: int = 0            # 증분 뒤 올려야 했던 파일 수(dry-run 이면 올릴 예정 수)
    manifest_failed: bool = False  # 올린 것을 사슬에 붙이지 못했습니다
    links: int = 0                 # 이번 실행에서 사슬에 붙은 고리 수
    preflight_failed: bool = False  # 릴리스에 닿지 못해 스캔도 하지 않았습니다
    aborted: bool = False          # 연속 실패로 중간에 멈췄습니다(나머지는 다음 실행)

    @property
    def ok(self) -> bool:
        """cli 종료 코드용. 묶음·매니페스트·사전 점검 실패가 있으면 False."""
        return (self.failed_bundles == 0 and not self.manifest_failed
                and not self.preflight_failed and not self.aborted)


def bundle_asset(prefix: str, seq: int, nonce: str, n: int) -> str:
    """nonce 는 실행마다 새로 뽑습니다. seq 만으로는 겹칠 수 있습니다 — 같은
    이름표의 PC 두 대, 매니페스트 실패 뒤 같은 seq 재시도, 상태 DB 분실.
    겹치면 업로더가 이미 사슬에 붙은 묶음을 지우고 교체합니다."""
    return f"{prefix}home-{seq:06d}-{nonce}-{n:03d}.zip"


def manifest_asset(prefix: str, seq: int, nonce: str) -> str:
    return f"{prefix}home-chain-{seq:06d}-{nonce}.json"


def new_nonce() -> str:
    return secrets.token_hex(3)


def _safe(text: str) -> str:
    """홀로 남은 서로게이트가 있어도 로그에 쓸 수 있게."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _clean_stale_tmp(tmp: Path) -> None:
    """지난 실행이 죽으며 남긴 home-* 임시 폴더(수 GB 짜리 zip)를 지웁니다.

    cli 가 배타 잠금을 잡고 부르므로 지금 다른 실행이 쓰는 폴더는 없습니다.
    """
    if not tmp.is_dir():
        return
    for old in tmp.glob("home-*"):
        log.info("지난 실행의 임시 파일을 지웁니다: %s", old.name)
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old, ignore_errors=True)
        else:
            try:
                old.unlink()
            except OSError:
                pass


def _group(rels: Iterable[str]) -> dict[str, list[str]]:
    """'a/b/c.txt' -> {'a/b/': ['c.txt']}. 홈 바로 아래 파일은 './'."""
    out: dict[str, list[str]] = {}
    for rel in sorted(rels):
        folder, _, name = rel.rpartition("/")
        out.setdefault(f"{folder}/" if folder else "./", []).append(name)
    return out


@dataclass
class _Sample:
    """개수와 앞 몇 개만 기억합니다(66만 개를 다 들고 있지 않게)."""

    title: str
    count: int = 0
    first: list[tuple[Path, str]] = field(default_factory=list)

    def add(self, path: Path, reason: str) -> None:
        self.count += 1
        if len(self.first) < _SHOW:
            self.first.append((path, reason))

    def log(self, home: Path) -> None:
        if not self.count:
            return
        log.info("%s %d건", self.title, self.count)
        for path, reason in self.first:
            try:
                shown = path.relative_to(home).as_posix()
            except ValueError:
                shown = str(path)
            log.info("  - %s (%s)", _safe(shown), reason)
        if self.count > len(self.first):
            log.info("  ... 외 %d건", self.count - len(self.first))


def _append_chain_log(path: Path, *, seq: int, tag: str, asset: str, sha: str,
                      created_at: str, entries: list[Entry], failed: int) -> None:
    lines = [f"== seq {seq}  {tag}  {asset}  {sha}  {created_at} =="]
    for folder, files in _group(e.path for e in entries).items():
        lines.append(folder)
        lines.extend(f"    {name}" for name in files)
    if failed:
        lines.append(f"(실패한 묶음 {failed}개 — 그 파일들은 다음 실행에서 다시 올립니다)")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


@dataclass
class _Link:
    """만들고 있는 고리 하나."""

    seq: int
    bundles: list[dict] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    rows: list[FileRow] = field(default_factory=list)
    failed: int = 0          # 이 고리 동안 실패한 묶음(체인 로그용)
    next_n: int = 1          # 묶음 번호. seq 안에서 겹치지 않게 계속 올라갑니다


def run_backup(*, home: Path, uploader, state: HomeState, layout: Layout, machine: str,
               prefix: str, tag: str, bundle_mb: int, exclude_names: list[str] | None,
               dry_run: bool, nonce: str | None = None) -> BackupResult:
    """prefix 와 machine 은 그대로 씁니다(cli 가 설치 id 를 넣어 줍니다)."""
    home = Path(home)
    nonce = nonce or new_nonce()
    log.info("홈 백업을 시작합니다: %s -> %s (실행 %s)%s", home, tag, nonce,
             " (dry-run)" if dry_run else "")

    # 에셋 이름을 우리가 정합니다. 업로더가 바꾸면 매니페스트와 어긋납니다.
    for probe in (bundle_asset(prefix, 0, nonce, 1), manifest_asset(prefix, 0, nonce)):
        if asset_name(probe) != probe:
            raise ValueError(f"에셋 이름이 업로더에서 바뀝니다: {probe!r} -> {asset_name(probe)!r}")

    excluded = _Sample("제외")
    unreadable = _Sample("읽을 수 없어 건너뜀")
    tally = {"skipped": 0, "todo": 0}

    def changed() -> Iterator[tuple[Path, int, int]]:
        """바뀐 파일만. 같은 크기·시각이면 해시도 계산하지 않습니다(경로 인덱스 조회)."""
        # gymhall 데이터 폴더가 홈 아래 있으면 뺍니다(녹화는 따로 올라가고, 정리도 보호합니다).
        for path, size, mtime_ns in iter_home(
                home, exclude_names=exclude_names, skip_dirs=[layout.root],
                on_excluded=excluded.add, on_unreadable=unreadable.add):
            row = state.get_file(path.relative_to(home).as_posix())
            if row is not None and row.size == size and row.mtime_ns == mtime_ns:
                tally["skipped"] += 1
                continue
            tally["todo"] += 1
            yield path, size, mtime_ns

    max_entries = max(1, int(MAX_LINK_ENTRIES))
    plans = plan_bundles(changed(), int(bundle_mb) * 1024 * 1024, max_files=max_entries)

    def result(**kw) -> BackupResult:
        excluded.log(home)
        unreadable.log(home)
        log.info("후보 중 새로 올릴 것 %d개, 바뀌지 않아 건너뜀 %d개",
                 tally["todo"], tally["skipped"])
        base = {
            "seq": None, "uploaded_files": 0, "skipped_unchanged": tally["skipped"],
            "failed_bundles": 0, "unreadable": unreadable.count,
            "excluded": excluded.count, "bytes": 0, "candidates": tally["todo"],
        }
        base.update(kw)
        return BackupResult(**base)

    if not dry_run:
        # 네트워크나 토큰이 고장이면 65GB 를 압축해 놓고 전부 실패합니다.
        # 스캔하기 전에 릴리스에 닿는지부터 봅니다.
        try:
            uploader.get_or_create_release(tag)
        except ReleaseError as exc:
            log.error("릴리스 %s 에 닿지 못했습니다. 스캔·압축 없이 멈춥니다: %s", tag, exc)
            return result(preflight_failed=True)
        _clean_stale_tmp(layout.tmp)

    head = state.get_head()
    prev_link: Link | None = head[0] if head else None
    first_seq = head[1] + 1 if head else 0

    if dry_run:
        # 상태는 읽기만 합니다(commit 없음). 네트워크·체인 로그도 없습니다.
        per_dir: dict[str, tuple[list[int], list[str]]] = {}   # 폴더 -> ([개수], 앞 20개)
        count = planned = n_bundles = 0
        for plan in plans:
            n_bundles += 1
            for path, size, _ in plan:
                count += 1
                planned += size
                folder, _, name = path.relative_to(home).as_posix().rpartition("/")
                counter, names = per_dir.setdefault(f"{folder}/" if folder else "./", ([0], []))
                counter[0] += 1
                if len(names) < 20:
                    names.append(name)
        links = -(-count // max_entries)
        log.info("[dry-run] seq %d 부터 고리 %d개, 묶음 %d개, %d개 파일(%d바이트)을 올릴 예정입니다.",
                 first_seq, links, n_bundles, count, planned)
        for folder in sorted(per_dir):
            (total,), names = per_dir[folder]
            more = total - len(names)
            log.info("  %s (%d개): %s%s", folder, total, ", ".join(names),
                     f" 외 {more}개" if more else "")
        return result(bytes=planned)

    layout.tmp.mkdir(parents=True, exist_ok=True)
    cur = _Link(seq=first_seq)
    last_seq: int | None = None
    links = uploaded_files = sent = failed_total = 0

    def close_link(work: Path) -> bool:
        """지금 고리의 매니페스트를 올리고, 성공하면 그때서야 commit 합니다."""
        nonlocal cur, prev_link, last_seq, links
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest = build_manifest(machine=machine, seq=cur.seq, prev=prev_link, tag=tag,
                                  home=str(home.resolve()), created_at=created_at,
                                  bundles=cur.bundles, entries=cur.entries)
        data = canonical_bytes(manifest)
        sha = digest(data)
        mname = manifest_asset(prefix, cur.seq, nonce)
        if exists(mname):
            return False
        mpath = work / mname
        mpath.write_bytes(data)
        try:
            uploader.upload(mpath, tag)
        except ReleaseError as exc:
            log.error("사슬 고리(매니페스트) 업로드 실패: %s -> %s. 이 고리의 로컬 상태는 "
                      "그대로 두고 여기서 멈춥니다.", mname, exc)
            return False
        finally:
            mpath.unlink(missing_ok=True)

        # 여기까지 왔으면 GitHub 에 고리가 있습니다. 이제서야 로컬을 바꿉니다.
        link = Link(tag, mname, sha)
        state.upsert_files(cur.rows)
        state.set_head(link, cur.seq)
        state.commit()
        log.info("사슬 고리 seq %d 를 올렸습니다: %s/%s (%s) — 파일 %d개, 묶음 %d개",
                 cur.seq, tag, mname, sha[:12], len(cur.entries), len(cur.bundles))
        # gymhall.log 에는 파일 수가 많은 폴더 20개까지만. 전체는 home-chain.log 에 있습니다.
        groups = sorted(_group(e.path for e in cur.entries).items(),
                        key=lambda kv: (-len(kv[1]), kv[0]))
        for folder, files in groups[:_LOG_FOLDERS]:
            log.info("  %s %d개", _safe(folder), len(files))
        if len(groups) > _LOG_FOLDERS:
            log.info("  ... 외 %d개 폴더 (전체 목록: %s)", len(groups) - _LOG_FOLDERS, CHAIN_LOG)
        _append_chain_log(layout.logs / CHAIN_LOG, seq=cur.seq, tag=tag, asset=mname,
                          sha=sha, created_at=created_at, entries=cur.entries,
                          failed=cur.failed)
        prev_link, last_seq = link, cur.seq
        links += 1
        cur = _Link(seq=cur.seq + 1)
        return True

    def exists(name: str) -> bool:
        """같은 이름이 이미 있으면 올리지 않습니다. 업로더는 같은 이름을 지우고
        교체하므로, 그게 이미 사슬에 붙은 것이면 정리가 유일한 사본을 지웁니다.

        분할되면 실제 에셋은 name.partNNN / name.parts.json 입니다. zip 이름만
        보면 분할 묶음끼리는 이 확인을 지나갑니다(검토에서 지적). 첫 조각과
        parts.json 까지 봅니다 — 조각 수가 달라도 첫 조각은 항상 있습니다.
        """
        for candidate in (name, f"{name}.part001", f"{name}.parts.json"):
            try:
                found = uploader.find_asset(tag, candidate)
            except ReleaseError as exc:
                log.error("에셋 목록을 확인하지 못해 올리지 않습니다: %s -> %s", candidate, exc)
                return True
            if found:
                log.error("같은 이름의 에셋이 이미 있습니다. 덮어쓰지 않고 실패로 셉니다: %s/%s",
                          tag, candidate)
                return True
        return False

    def stop(**kw) -> BackupResult:
        return result(seq=last_seq, uploaded_files=uploaded_files, bytes=sent,
                      failed_bundles=failed_total, links=links, **kw)

    consecutive = 0
    aborted = False
    with tempfile.TemporaryDirectory(dir=layout.tmp, prefix="home-") as td:
        work = Path(td)
        for plan in plans:
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log.error("묶음이 %d번 연달아 실패했습니다. 나머지는 다음 실행에 맡기고 멈춥니다.",
                          consecutive)
                aborted = True
                break
            # 이 묶음을 넣으면 상한을 넘으면, 먼저 지금 고리를 닫습니다.
            # 묶음 이름에 seq 가 들어가므로 올리기 전에 정해야 합니다.
            if (cur.entries and len(cur.entries) + len(plan) > max_entries
                    and not close_link(work)):
                return stop(manifest_failed=True)
            name = bundle_asset(prefix, cur.seq, nonce, cur.next_n)
            cur.next_n += 1
            if exists(name):
                cur.failed += 1
                failed_total += 1
                consecutive += 1
                continue
            zpath = work / name
            try:
                out = write_bundle(home, plan, zpath)
            except OSError as exc:
                # 디스크가 꽉 찬 경우 등. 예전에는 여기서 실행 전체가 죽었고,
                # 이미 올린 묶음은 사슬에 안 붙은 채 매일 같은 자리에서 다시
                # 죽었습니다(검토에서 재현). 이 묶음만 실패로 세고 계속합니다.
                zpath.unlink(missing_ok=True)
                cur.failed += 1
                failed_total += 1
                consecutive += 1
                log.error("묶음을 만들지 못했습니다: %s (%d개 파일) -> %s", name, len(plan), exc)
                continue
            for path, reason in out.unreadable:
                unreadable.add(path, reason)
            if not out.packed:
                zpath.unlink(missing_ok=True)
                continue
            zsize = zpath.stat().st_size
            try:
                # 분할 조각도 이 실행의 임시 폴더에 둡니다(업로더가 끝나면 지웁니다).
                res = uploader.upload(zpath, tag, tmp_dir=work)
            except (ReleaseError, OSError) as exc:
                # OSError: 분할 조각을 쓰다 디스크가 꽉 찬 경우(원본의 두 배가 필요).
                zpath.unlink(missing_ok=True)
                cur.failed += 1
                failed_total += 1
                consecutive += 1
                log.error("묶음 업로드 실패: %s (%d개 파일) -> %s", name, len(out.packed), exc)
                continue
            consecutive = 0
            zsha = str(res.get("sha256") or "") or sha256_file(zpath)
            zpath.unlink(missing_ok=True)
            bundle: dict = {"asset": name, "size": zsize, "sha256": zsha, "parts": 0}
            if res.get("split"):
                part_info = list(res.get("parts") or [])
                bundle["parts"] = len(part_info)
                # 조각별 해시를 사슬에 넣습니다. 정리는 parts.json(덮일 수 있음)
                # 이 아니라 이 값과 GitHub digest 를 맞춥니다.
                bundle["part_sha256"] = [str(p.get("sha256", "")) for p in part_info]
            parts = bundle["parts"]
            cur.bundles.append(bundle)
            # 캐시에는 스캔 때 본 크기/시각을 둡니다. 다음 스캔과 같은 기준으로
            # 비교해야 합니다(읽는 도중 파일이 자랐다면 다음에 다시 올라갑니다).
            scanned = {path: size for path, size, _ in plan}
            for p in out.packed:
                cur.entries.append(Entry(path=p.member, size=p.size, mtime_ns=p.mtime_ns,
                                         sha256=p.sha256, bundle=name, member=p.member))
                cur.rows.append(FileRow(p.member, scanned.get(p.path, p.size),
                                        p.mtime_ns, p.sha256))
                sent += p.size
            uploaded_files += len(out.packed)
            log.info("묶음 올림: %s (%d개 파일, %d바이트%s)", name, len(out.packed), zsize,
                     f", {parts}조각" if parts else "")
            # 꽉 찼으면 다음 묶음을 기다리지 않고 바로 닫습니다. 다음 묶음을
            # 만들려면 스캔을 더 해야 하는데, 그사이 죽으면 이 고리가 날아갑니다.
            if len(cur.entries) >= max_entries and not close_link(work):
                return stop(manifest_failed=True)

        # 멈췄더라도 이미 올라간 묶음은 사슬에 붙입니다.
        if cur.entries and not close_link(work):
            return stop(manifest_failed=True, aborted=aborted)

    if not links and tally["todo"] == 0:
        log.info("올릴 새 파일이 없습니다. 사슬 고리를 만들지 않습니다.")
    elif not links and failed_total:
        log.error("올라간 묶음이 없습니다(실패 %d). 사슬 고리를 만들지 않습니다.", failed_total)
        return stop(manifest_failed=True, aborted=aborted)
    return stop(aborted=aborted)
