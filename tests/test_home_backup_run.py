"""홈 백업 한 번 돌리기.

가장 중요한 약속: **GitHub 에 사슬 고리가 올라간 뒤에만** 로컬 상태가
바뀝니다. 로컬만 '올렸다' 고 믿으면 정리가 백업 없는 원본을 지웁니다.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest

from gymhall.backup import bundle as bundle_mod
from gymhall.backup.bundle import plan_bundles, write_bundle
from gymhall.backup.chain import (
    Link,
    entries_of,
    parse_manifest,
    walk_chain,
)
from gymhall.backup.homestate import HomeState
from gymhall.backup.run import CHAIN_LOG, run_backup
from gymhall.backup.scan import DEFAULT_EXCLUDE_NAMES
from gymhall.paths import Layout
from gymhall.release import ReleaseError
from gymhall.state import sha256_file

MiB = 1024 * 1024
PREFIX = "pc1_"
MACHINE = "pc1"


class FakeUploader:
    """실제 ReleaseUploader.upload 와 같은 모양을 돌려주고, 올린 바이트를 보관합니다."""

    def __init__(self, fail: set[str] | None = None, split_over: int = 0,
                 release_error: bool = False):
        self.fail = set(fail or ())
        self.split_over = split_over
        self.release_error = release_error
        self.calls: list[tuple[str, str]] = []
        self.upload_kw: list[dict] = []
        self.assets: dict[tuple[str, str], bytes] = {}
        self.releases: list[str] = []
        self.runs = 0

    def get_or_create_release(self, tag: str, **kw) -> dict:
        if self.release_error:
            raise ReleaseError("가짜: 토큰이 거부됐습니다")
        if tag not in self.releases:
            self.releases.append(tag)
        return {"id": 1, "tag_name": tag}

    def find_asset(self, tag: str, name: str) -> dict | None:
        return {"name": name} if (tag, name) in self.assets else None

    def upload(self, path: Path, tag: str, **kw) -> dict:
        path = Path(path)
        name = path.name
        self.calls.append((tag, name))
        self.upload_kw.append(kw)
        if any(f in name for f in self.fail):
            raise ReleaseError(f"가짜 실패: {name}")
        self.get_or_create_release(tag)
        data = path.read_bytes()
        self.assets[(tag, name)] = data
        digest = sha256_file(path)
        if self.split_over and len(data) > self.split_over:
            assets = [{"name": f"{name}.part{i:03d}", "size": 1} for i in (1, 2)]
            assets.append({"name": f"{name}.parts.json", "size": 1})
            parts = [{"name": f"{name}.part{i:03d}", "size": 1,
                      "sha256": f"{i:x}" * 64} for i in (1, 2)]
            return {"tag": tag, "assets": assets, "split": True, "sha256": digest,
                    "parts": parts}
        return {"tag": tag, "assets": [{"name": name, "size": len(data)}],
                "split": False, "sha256": digest}

    def fetch(self, link: Link) -> bytes:
        return self.assets[(link.tag, link.asset)]

    def names(self) -> list[str]:
        return [n for _, n in self.calls]


def _w(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    layout = Layout(tmp_path / "data")
    layout.ensure()
    with HomeState.open(layout.state / "home_backup.sqlite") as state:
        yield home, layout, state


def _run(env, up, *, tag="home-2026-11-13", bundle_mb=1800, dry_run=False, nonce=None):
    """실행마다 nonce 를 n1, n2, ... 로 정해 이름을 예측할 수 있게 합니다."""
    home, layout, st = env
    up.runs += 1
    return run_backup(home=home, uploader=up, state=st, layout=layout,
                      machine=MACHINE, prefix=PREFIX, tag=tag, bundle_mb=bundle_mb,
                      exclude_names=list(DEFAULT_EXCLUDE_NAMES), dry_run=dry_run,
                      nonce=nonce or f"n{up.runs}")


def _head(state: HomeState) -> Link:
    return state.get_head()[0]


def _seq(state: HomeState) -> int:
    return state.get_head()[1]


def _paths(state: HomeState) -> set[str]:
    return {r[0] for r in state.conn.execute("SELECT path FROM files")}


def _fresh(layout: Layout) -> HomeState:
    """다른 연결로 엽니다. commit 된 것만 보입니다."""
    return HomeState.open(layout.state / "home_backup.sqlite")


def _members(up: FakeUploader, tag: str, name: str) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(up.assets[(tag, name)])) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ---------------------------------------------------------------------------
def test_first_run_makes_seq_0_without_prev(env):
    home, layout, store = env
    _w(home / "Documents/과제/보고서.hwp", b"hello")
    _w(home / "a.txt", b"top")
    up = FakeUploader()
    res = _run(env, up)

    assert res.seq == 0 and res.uploaded_files == 2 and res.failed_bundles == 0 and res.ok
    assert up.names() == ["pc1_home-000000-n1-001.zip", "pc1_home-chain-000000-n1.json"]
    data = up.assets[("home-2026-11-13", "pc1_home-chain-000000-n1.json")]
    with _fresh(layout) as other:                                  # commit 됐는지
        head = _head(other)
    m = parse_manifest(data, expect=head)
    assert m["seq"] == 0 and m["prev"] is None and m["machine"] == MACHINE
    assert _seq(store) == 0
    members = _members(up, "home-2026-11-13", "pc1_home-000000-n1-001.zip")
    assert members == {"Documents/과제/보고서.hwp": b"hello", "a.txt": b"top"}


def test_entries_hash_and_size_match_the_bytes_in_the_zip(env):
    home, _, store = env
    payload = os.urandom(3 * MiB + 17)
    _w(home / "big/blob.bin", payload)
    up = FakeUploader()
    _run(env, up)
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    (e,) = entries_of(m)
    import hashlib

    assert e.sha256 == hashlib.sha256(payload).hexdigest() and e.size == len(payload)
    assert e.bundle == "pc1_home-000000-n1-001.zip" and e.member == "big/blob.bin"
    assert _members(up, "home-2026-11-13", e.bundle)[e.member] == payload
    b = m["bundles"][0]
    zip_bytes = up.assets[("home-2026-11-13", e.bundle)]
    assert b["size"] == len(zip_bytes) and b["parts"] == 0
    assert b["sha256"] == hashlib.sha256(zip_bytes).hexdigest()
    # 캐시도 같은 해시를 기억합니다
    assert store.get_file("big/blob.bin").sha256 == e.sha256


def test_second_run_uploads_only_the_changed_file_and_links_to_the_first(env):
    home, layout, store = env
    _w(home / "a.txt", b"one")
    f = _w(home / "b.txt", b"two")
    up = FakeUploader()
    _run(env, up, tag="home-2026-11-13")
    first_bytes = up.assets[("home-2026-11-13", "pc1_home-chain-000000-n1.json")]

    f.write_bytes(b"two-changed")
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 5_000_000_000))
    res = _run(env, up, tag="home-2026-11-14")

    assert res.seq == 1 and res.uploaded_files == 1 and res.skipped_unchanged == 1
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert [e.path for e in entries_of(m)] == ["b.txt"]
    import hashlib

    assert m["prev"]["sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert m["prev"]["asset"] == "pc1_home-chain-000000-n1.json"
    assert m["prev"]["tag"] == "home-2026-11-13"
    assert _members(up, "home-2026-11-14", "pc1_home-000001-n2-001.zip") == {
        "b.txt": b"two-changed"}

    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [c["seq"] for c in chain] == [1, 0]


def test_the_uploaded_chain_round_trips_through_walk_chain(env):
    home, _, store = env
    up = FakeUploader()
    for i in range(3):
        _w(home / f"d{i}/f.txt", f"v{i}".encode())
        _run(env, up, tag=f"home-2026-11-1{i}")
    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [c["seq"] for c in chain] == [2, 1, 0]
    paths = [e.path for c in chain for e in entries_of(c)]
    assert sorted(paths) == ["d0/f.txt", "d1/f.txt", "d2/f.txt"]
    # created_at 은 시간대가 붙은 로컬 ISO 입니다(정리가 나이를 계산합니다).
    from datetime import datetime

    assert datetime.fromisoformat(chain[0]["created_at"]).tzinfo is not None


def test_unchanged_files_are_skipped_without_hashing(env, monkeypatch):
    home, _, _ = env
    _w(home / "a.txt", b"one")
    up = FakeUploader()
    _run(env, up)
    calls_before = len(up.calls)

    def boom(*a, **k):
        raise AssertionError("바뀌지 않은 파일을 다시 읽었습니다")

    monkeypatch.setattr(bundle_mod, "write_bundle", boom)
    import gymhall.backup.run as run_mod

    monkeypatch.setattr(run_mod, "write_bundle", boom)
    monkeypatch.setattr(run_mod, "sha256_file", boom)
    res = _run(env, up, tag="home-2026-11-14")
    assert res.seq is None and res.skipped_unchanged == 1 and res.uploaded_files == 0
    assert len(up.calls) == calls_before, "올릴 게 없으면 고리도 만들지 않습니다"
    assert res.ok


def test_failed_bundle_is_left_out_of_the_manifest_and_retried_next_time(env):
    home, _, store = env
    _w(home / "a/1.bin", b"a" * (MiB // 2 + 1))
    _w(home / "b/2.bin", b"b" * (MiB // 2 + 1))
    up = FakeUploader(fail={"-002.zip"})
    res = _run(env, up, bundle_mb=1)

    assert res.failed_bundles == 1 and res.seq == 0 and not res.ok
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert [e.path for e in entries_of(m)] == ["a/1.bin"]
    assert [b["asset"] for b in m["bundles"]] == ["pc1_home-000000-n1-001.zip"]
    assert _paths(store) == {"a/1.bin"}, "실패한 묶음의 파일을 기억하면 안 됩니다"

    up.fail.clear()
    res2 = _run(env, up, tag="home-2026-11-14", bundle_mb=1)
    assert res2.seq == 1 and res2.uploaded_files == 1 and res2.skipped_unchanged == 1
    m2 = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert [e.path for e in entries_of(m2)] == ["b/2.bin"]


def test_all_bundles_failing_makes_no_manifest_and_is_a_failure(env):
    home, layout, store = env
    _w(home / "a.txt")
    up = FakeUploader(fail={".zip"})
    res = _run(env, up)
    assert res.seq is None and res.failed_bundles == 1 and res.manifest_failed and not res.ok
    assert store.get_head() is None and store.file_count() == 0
    assert not any("chain" in n for n in up.names())
    assert not (layout.logs / CHAIN_LOG).exists()


def test_manifest_upload_failure_leaves_head_and_cache_unchanged(env):
    home, layout, store = env
    _w(home / "a.txt", b"1")
    up = FakeUploader()
    _run(env, up)
    head_before = store.get_head()
    files_before = _paths(store)
    log_before = (layout.logs / CHAIN_LOG).read_text(encoding="utf-8")

    _w(home / "new.txt", b"2")
    up.fail = {"home-chain-"}
    res = _run(env, up, tag="home-2026-11-14")
    assert res.manifest_failed and not res.ok and res.seq is None
    # cli 는 'uploaded_files 가 있는데 seq 가 None' 을 실패로 봅니다.
    assert res.uploaded_files == 1
    assert store.get_head() == head_before
    assert _paths(store) == files_before
    with _fresh(layout) as other:
        assert other.get_head() == head_before and _paths(other) == files_before
    assert (layout.logs / CHAIN_LOG).read_text(encoding="utf-8") == log_before

    # 다음 실행은 같은 seq 로 다시 시도하고 새 파일을 다시 올립니다.
    up.fail.clear()
    res2 = _run(env, up, tag="home-2026-11-15")
    assert res2.seq == 1 and res2.uploaded_files == 1


def test_a_file_bigger_than_bundle_mb_goes_alone(env):
    home, _, store = env
    _w(home / "a/small1.txt", b"s")
    _w(home / "b/huge.bin", b"h" * (2 * MiB))
    _w(home / "c/small2.txt", b"t")
    up = FakeUploader()
    _run(env, up, bundle_mb=1)
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    by_bundle: dict[str, list[str]] = {}
    for e in entries_of(m):
        by_bundle.setdefault(e.bundle, []).append(e.path)
    assert sorted(by_bundle.values()) == [["a/small1.txt"], ["b/huge.bin"], ["c/small2.txt"]]


def test_split_bundles_record_their_part_count(env):
    home, _, store = env
    _w(home / "x.bin", os.urandom(200_000))
    up = FakeUploader(split_over=1000)
    _run(env, up)
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert m["bundles"][0]["parts"] == 2


def test_excluded_things_never_reach_the_zip(env):
    home, _, store = env
    _w(home / "AppData/Local/x.db")
    _w(home / ".ssh/known_hosts")
    _w(home / "Documents/id_rsa")
    _w(home / "Documents/.env")
    _w(home / "proj/.git/HEAD", b"ref")
    _w(home / "Documents/ok.txt")
    up = FakeUploader()
    res = _run(env, up)
    members = _members(up, "home-2026-11-13", "pc1_home-000000-n1-001.zip")
    assert set(members) == {"proj/.git/HEAD", "Documents/ok.txt"}
    assert res.excluded >= 4


def test_the_gymhall_data_folder_under_home_is_not_backed_up(tmp_path):
    home = tmp_path / "home"
    layout = Layout(home / "gymhall-data")
    layout.ensure()
    _w(layout.recordings / "2026-11-13/a.mkv")
    _w(home / "Documents/a.txt")
    up = FakeUploader()
    with HomeState.open(layout.state / "home_backup.sqlite") as state:
        run_backup(home=home, uploader=up, state=state, layout=layout, machine=MACHINE,
                       prefix=PREFIX, tag="t", bundle_mb=10, exclude_names=[], dry_run=False,
                   nonce="x")
    assert set(_members(up, "t", "pc1_home-000000-x-001.zip")) == {"Documents/a.txt"}


def test_dry_run_touches_no_network_state_or_chain_log(env, caplog):
    home, layout, store = env
    _w(home / "Documents/과제/a.hwp")
    up = FakeUploader()
    with caplog.at_level("INFO", logger="gymhall"):
        res = _run(env, up, dry_run=True)
    assert up.calls == [] and up.releases == []
    assert store.get_head() is None and store.file_count() == 0
    assert not store.conn.in_transaction, "dry-run 이 쓰기 트랜잭션을 열었습니다"
    assert not (layout.logs / CHAIN_LOG).exists()
    assert res.seq is None and res.candidates == 1 and res.ok
    assert "Documents/과제/" in caplog.text and "a.hwp" in caplog.text


def test_chain_log_lists_files_grouped_by_directory(env):
    home, layout, store = env
    _w(home / "Documents/과제/a.hwp")
    _w(home / "Documents/과제/b.hwp")
    _w(home / "Pictures/c.png")
    _w(home / "top.txt")
    up = FakeUploader()
    _run(env, up)
    text = (layout.logs / CHAIN_LOG).read_text(encoding="utf-8")
    head = _head(store)
    assert f"== seq 0  home-2026-11-13  pc1_home-chain-000000-n1.json  {head.sha256}" in text
    lines = text.splitlines()
    i = lines.index("Documents/과제/")
    assert lines[i + 1].strip() == "a.hwp" and lines[i + 2].strip() == "b.hwp"
    assert "Pictures/" in lines and "./" in lines

    _w(home / "Pictures/d.png")
    _run(env, up, tag="home-2026-11-14")
    text2 = (layout.logs / CHAIN_LOG).read_text(encoding="utf-8")
    assert text2.startswith(text), "append 여야 합니다"
    assert "== seq 1  home-2026-11-14" in text2


def test_unreadable_file_is_left_out_and_counted(env, monkeypatch):
    home, _, store = env
    _w(home / "locked.txt", b"no")
    _w(home / "ok.txt", b"yes")
    real_open = Path.open

    def fake_open(self, *a, **k):
        if self.name == "locked.txt":
            raise PermissionError("locked")
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", fake_open)
    res = _run(env, up := FakeUploader())
    monkeypatch.setattr(Path, "open", real_open)
    assert res.unreadable == 1 and res.ok and res.uploaded_files == 1
    assert set(_members(up, "home-2026-11-13", "pc1_home-000000-n1-001.zip")) == {"ok.txt"}
    assert _paths(store) == {"ok.txt"}


def test_read_error_midway_rebuilds_the_bundle_without_the_file(tmp_path, monkeypatch):
    """반쯤 쓴 항목이 zip 에 남으면 안 됩니다."""
    home = tmp_path / "h"
    a = _w(home / "a.bin", os.urandom(3 * MiB))
    b = _w(home / "b.bin", b"fine")
    real_open = Path.open

    class Flaky(io.RawIOBase):
        def __init__(self, fh):
            self.fh, self.n = fh, 0

        def read(self, size=-1):
            self.n += 1
            if self.n == 2:
                raise OSError("disk went away")
            return self.fh.read(size)

        def close(self):
            self.fh.close()
            super().close()

    def fake_open(self, *args, **kw):
        fh = real_open(self, *args, **kw)
        return Flaky(fh) if self.name == "a.bin" else fh

    monkeypatch.setattr(Path, "open", fake_open)
    files = [(a, a.stat().st_size, a.stat().st_mtime_ns), (b, 4, b.stat().st_mtime_ns)]
    out = write_bundle(home, files, tmp_path / "out.zip")
    monkeypatch.setattr(Path, "open", real_open)
    assert [p.member for p in out.packed] == ["b.bin"]
    assert [p.name for p, _ in out.unreadable] == ["a.bin"]
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert zf.namelist() == ["b.bin"]
        assert zf.testzip() is None


def test_plan_bundles_respects_the_limit():
    p = Path
    files = [(p("a"), 4, 0), (p("b"), 4, 0), (p("c"), 3, 0), (p("d"), 20, 0), (p("e"), 1, 0)]
    plans = list(plan_bundles(files, 10))
    assert [[f[0].name for f in plan] for plan in plans] == [["a", "b"], ["c"], ["d"], ["e"]]
    assert all(sum(f[1] for f in plan) <= 10 or len(plan) == 1 for plan in plans)


def test_compressed_types_are_stored_and_others_deflated(tmp_path):
    home = tmp_path / "h"
    a = _w(home / "v.mp4", b"0" * 5000)
    b = _w(home / "t.txt", b"0" * 5000)
    files = [(f, f.stat().st_size, f.stat().st_mtime_ns) for f in (a, b)]
    write_bundle(home, files, tmp_path / "o.zip")
    with zipfile.ZipFile(tmp_path / "o.zip") as zf:
        assert zf.getinfo("v.mp4").compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("t.txt").compress_type == zipfile.ZIP_DEFLATED
        assert zf.getinfo("t.txt").compress_size < 5000


def test_bundle_reads_in_chunks_not_whole_files(tmp_path, monkeypatch):
    home = tmp_path / "h"
    f = _w(home / "big.bin", os.urandom(3 * MiB + 5))
    sizes: list[int] = []
    real_open = Path.open

    class Spy(io.RawIOBase):
        def __init__(self, fh):
            self.fh = fh

        def read(self, size=-1):
            sizes.append(size)
            return self.fh.read(size)

        def close(self):
            self.fh.close()
            super().close()

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k))
                        if self.name == "big.bin" else real_open(self, *a, **k))
    write_bundle(home, [(f, f.stat().st_size, f.stat().st_mtime_ns)], tmp_path / "o.zip")
    monkeypatch.setattr(Path, "open", real_open)
    assert sizes and all(0 < s <= MiB for s in sizes), sizes


# ---------------------------------------------------------------------------
# 66만 개 규모: 고리 상한, 고리마다 commit, 흘려보내기
# ---------------------------------------------------------------------------
@pytest.fixture
def small_links(monkeypatch):
    import gymhall.backup.run as run_mod

    monkeypatch.setattr(run_mod, "MAX_LINK_ENTRIES", 3)
    return 3


def test_cap_splits_one_run_into_chained_links(env, small_links):
    home, layout, store = env
    for i in range(8):
        _w(home / f"d/{i:02d}.txt", f"v{i}".encode())
    up = FakeUploader()
    res = _run(env, up)
    assert res.links == 3 and res.seq == 2 and res.uploaded_files == 8 and res.ok
    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [c["seq"] for c in chain] == [2, 1, 0]
    assert all(len(c["entries"]) <= 3 for c in chain)
    assert sorted(e.path for c in chain for e in entries_of(c)) == [
        f"d/{i:02d}.txt" for i in range(8)]
    # 고리마다 묶음 이름의 seq 가 다르므로 겹치지 않습니다.
    zips = [n for n in up.names() if n.endswith(".zip")]
    assert len(zips) == len(set(zips))
    assert {b["asset"].split("-")[1] for b in chain[0]["bundles"]} == {"000002"}
    assert store.file_count() == 8
    text = (layout.logs / CHAIN_LOG).read_text(encoding="utf-8")
    assert text.count("== seq ") == 3


def test_manifest_failure_on_link_2_keeps_link_1(env, small_links):
    home, layout, store = env
    for i in range(7):
        _w(home / f"d/{i:02d}.txt", f"v{i}".encode())
    up = FakeUploader(fail={"home-chain-000001"})
    res = _run(env, up)
    assert res.manifest_failed and not res.ok
    assert res.seq == 0 and res.links == 1
    assert res.uploaded_files == 6, "올라간 묶음의 파일은 세어야 합니다"
    assert not any("home-000002" in n for n in up.names()), "실패한 고리 뒤로 이어 가면 안 됩니다"
    # 실패한 고리에서 바로 멈춥니다: 마지막 호출이 그 매니페스트이고, 한 번만 시도했습니다.
    assert up.names()[-1] == "pc1_home-chain-000001-n1.json"
    assert up.names().count("pc1_home-chain-000001-n1.json") == 1
    with _fresh(layout) as other:
        link, seq = other.get_head()
        assert seq == 0 and link.asset == "pc1_home-chain-000000-n1.json"
        assert _paths(other) == {"d/00.txt", "d/01.txt", "d/02.txt"}
    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [c["seq"] for c in chain] == [0]

    # 다음 실행은 seq 1 부터 나머지를 올립니다.
    up.fail.clear()
    res2 = _run(env, up, tag="home-2026-11-14")
    assert res2.ok and res2.skipped_unchanged == 3 and res2.uploaded_files == 4
    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [c["seq"] for c in chain] == [2, 1, 0]


def test_a_link_never_exceeds_the_cap_when_bundles_do_not_fill_it(env, small_links):
    """묶음이 2개씩이면 2+2=4 > 3 — 넣기 전에 고리를 닫아야 합니다."""
    home, _, store = env
    for i in range(6):
        _w(home / f"d/{i:02d}.bin", bytes([i]) * (400 * 1024))
    up = FakeUploader()
    res = _run(env, up, bundle_mb=1)
    chain = walk_chain(_head(store), up.fetch, machine=MACHINE)
    assert [len(c["entries"]) for c in reversed(chain)] == [2, 2, 2]
    assert res.links == 3 and res.ok


def test_a_run_that_dies_midway_keeps_completed_links(env, small_links, monkeypatch):
    """스캔 도중 죽어도(전원, 예외) 이미 닫힌 고리는 commit 돼 있어야 합니다."""
    import gymhall.backup.run as run_mod

    home, layout, store = env
    real = run_mod.iter_home

    def dying(*a, **k):
        for i, item in enumerate(real(*a, **k)):
            if i == 5:
                raise RuntimeError("전원이 나갔습니다")
            yield item

    for i in range(9):
        _w(home / f"d/{i:02d}.txt", f"v{i}".encode())
    monkeypatch.setattr(run_mod, "iter_home", dying)
    up = FakeUploader()
    with pytest.raises(RuntimeError):
        _run(env, up)
    with _fresh(layout) as other:
        assert other.get_head()[1] == 0, "첫 고리는 스캔이 끝나기 전에 닫혀야 합니다"
        assert other.file_count() == 3


def test_plan_bundles_is_lazy():
    from collections.abc import Iterator

    pulled: list[int] = []

    def gen():
        for i in range(10):
            pulled.append(i)
            if i == 6:
                raise AssertionError("묶음 하나를 내기 위해 전부 읽었습니다")
            yield (Path(f"f{i}"), 4, 0)

    it = plan_bundles(gen(), 10)
    assert isinstance(it, Iterator)
    assert pulled == []
    assert [p.name for p, _, _ in next(it)] == ["f0", "f1"]
    assert len(pulled) <= 3


def test_plan_bundles_caps_file_count():
    files = [(Path(f"f{i}"), 1, 0) for i in range(7)]
    assert [len(b) for b in plan_bundles(files, 10**9, max_files=3)] == [3, 3, 1]


def test_incremental_lookup_is_per_path_not_a_full_load(env, monkeypatch):
    home, _, store = env
    for i in range(4):
        _w(home / f"{i}.txt")
    up = FakeUploader()
    _run(env, up)
    seen: list[str] = []
    real = store.get_file
    monkeypatch.setattr(store, "get_file", lambda p: seen.append(p) or real(p))
    res = _run(env, up, tag="home-2026-11-14")
    assert res.skipped_unchanged == 4 and sorted(seen) == ["0.txt", "1.txt", "2.txt", "3.txt"]


# ---------------------------------------------------------------------------
# 리뷰 반영: 이름 충돌, 사전 점검, 연속 실패, 임시 폴더, 서로게이트
# ---------------------------------------------------------------------------
def test_names_carry_a_per_run_nonce(env):
    from gymhall.backup.run import bundle_asset, manifest_asset, new_nonce

    assert bundle_asset("p_", 3, "ab12cd", 7) == "p_home-000003-ab12cd-007.zip"
    assert manifest_asset("p_", 3, "ab12cd") == "p_home-chain-000003-ab12cd.json"
    a, b = new_nonce(), new_nonce()
    assert a != b and len(a) == 6 and a.isalnum()

    home, layout, st = env
    _w(home / "a.txt")
    up = FakeUploader()
    res = run_backup(home=home, uploader=up, state=st, layout=layout, machine=MACHINE,
                     prefix=PREFIX, tag="t", bundle_mb=10, exclude_names=[], dry_run=False)
    (bundle,) = [n for n in up.names() if n.endswith(".zip")]
    nonce = bundle.split("-")[2]
    assert len(nonce) == 6 and res.ok
    assert up.names()[-1] == f"pc1_home-chain-000000-{nonce}.json"


def test_an_existing_bundle_name_is_never_replaced(env):
    """업로더는 같은 이름을 지우고 교체합니다. 이미 사슬에 붙은 것이면 유일한 사본이 사라집니다."""
    home, _, store = env
    _w(home / "a.txt", b"new")
    up = FakeUploader()
    up.assets[("home-2026-11-13", "pc1_home-000000-n1-001.zip")] = b"chained-elsewhere"
    res = _run(env, up)
    assert "pc1_home-000000-n1-001.zip" not in up.names(), "기존 에셋에 upload 를 불렀습니다"
    assert up.assets[("home-2026-11-13", "pc1_home-000000-n1-001.zip")] == b"chained-elsewhere"
    assert res.failed_bundles == 1 and not res.ok
    assert store.get_head() is None and store.file_count() == 0


def test_an_existing_manifest_name_is_never_replaced(env):
    home, _, store = env
    _w(home / "a.txt", b"new")
    up = FakeUploader()
    up.assets[("home-2026-11-13", "pc1_home-chain-000000-n1.json")] = b"other chain"
    res = _run(env, up)
    assert "pc1_home-chain-000000-n1.json" not in up.names()
    assert res.manifest_failed and not res.ok and res.uploaded_files == 1
    assert store.get_head() is None


def test_unreachable_release_stops_before_scanning(env, monkeypatch):
    import gymhall.backup.run as run_mod

    home, _, _ = env
    _w(home / "a.txt")

    def no_scan(*a, **k):
        raise AssertionError("릴리스에 닿지 못했는데 스캔했습니다")

    monkeypatch.setattr(run_mod, "iter_home", no_scan)
    monkeypatch.setattr(run_mod, "write_bundle", no_scan)
    up = FakeUploader(release_error=True)
    res = _run(env, up)
    assert res.preflight_failed and not res.ok and up.calls == []


def test_dry_run_does_not_need_the_release(env):
    home, _, _ = env
    _w(home / "a.txt")
    up = FakeUploader(release_error=True)
    res = _run(env, up, dry_run=True)
    assert res.ok and res.candidates == 1


def test_three_consecutive_failures_abort_but_chain_what_went_up(env, monkeypatch):
    import gymhall.backup.run as run_mod

    home, _, store = env
    for i in range(8):
        _w(home / f"d/{i}.bin", bytes([i]) * (MiB // 2 + 1))    # 묶음 하나에 하나씩
    zipped: list[str] = []
    real = run_mod.write_bundle

    def spy(h, plan, dest):
        zipped.append(Path(dest).name)
        return real(h, plan, dest)

    monkeypatch.setattr(run_mod, "write_bundle", spy)
    up = FakeUploader(fail={"-002.zip", "-003.zip", "-004.zip"})
    res = _run(env, up, bundle_mb=1)
    assert res.aborted and not res.ok and res.failed_bundles == 3
    assert len(zipped) == 4, "세 번 연달아 실패한 뒤로는 압축하지 않아야 합니다"
    assert res.seq == 0 and res.uploaded_files == 1
    assert {r[0] for r in store.conn.execute("SELECT path FROM files")} == {"d/0.bin"}


def test_a_success_resets_the_failure_streak(env):
    home, _, _ = env
    for i in range(6):
        _w(home / f"d/{i}.bin", bytes([i]) * (MiB // 2 + 1))
    up = FakeUploader(fail={"-001.zip", "-002.zip", "-004.zip", "-005.zip"})
    res = _run(env, up, bundle_mb=1)
    assert not res.aborted and res.failed_bundles == 4 and res.uploaded_files == 2


def test_stale_temp_leftovers_are_removed_and_parts_stay_in_the_run_dir(env):
    home, layout, _ = env
    stale = layout.tmp / "home-deadbeef"
    _w(stale / "pc1_home-000000-x-001.zip", b"z" * 1000)
    _w(stale / ".parts-x" / "a.part001", b"p")
    other = _w(layout.tmp / "keep-me.txt", b"k")
    _w(home / "a.txt")
    up = FakeUploader()
    _run(env, up)
    assert not stale.exists()
    assert other.exists(), "home-* 가 아닌 것은 건드리지 않습니다"
    tmp_dirs = {Path(kw["tmp_dir"]).parent for kw in up.upload_kw if "tmp_dir" in kw}
    assert tmp_dirs == {layout.tmp}
    assert list(layout.tmp.glob("home-*")) == [], "이번 실행의 임시 폴더도 지워져야 합니다"


def test_dry_run_leaves_temp_alone(env):
    home, layout, _ = env
    stale = _w(layout.tmp / "home-old" / "x.zip")
    _w(home / "a.txt")
    _run(env, FakeUploader(), dry_run=True)
    assert stale.exists()


def test_manifest_home_is_the_resolved_path(env):
    """정리가 이 값과 자기 홈을 비교합니다. '..' 같은 표기 차이로 어긋나면 안 됩니다."""
    home, layout, store = env
    _w(home / "a.txt")
    (home.parent / "x").mkdir()
    roundabout = home.parent / "x" / ".." / home.name
    up = FakeUploader()
    run_backup(home=roundabout, uploader=up, state=store, layout=layout, machine=MACHINE,
               prefix=PREFIX, tag="t", bundle_mb=10, exclude_names=[], dry_run=False)
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert m["home"] == str(home.resolve()) and ".." not in m["home"]


def test_each_run_gets_a_fresh_nonce_by_default(env):
    home, layout, store = env
    up = FakeUploader()
    for i in range(2):
        _w(home / f"{i}.txt")
        run_backup(home=home, uploader=up, state=store, layout=layout, machine=MACHINE,
                   prefix=PREFIX, tag="t", bundle_mb=10, exclude_names=[], dry_run=False)
    nonces = {n.split("-")[2] for n in up.names() if n.endswith(".zip")}
    assert len(nonces) == 2


def test_a_lone_surrogate_name_does_not_kill_the_run(env, monkeypatch, caplog):
    import gymhall.backup.scan as scan_mod

    home, _, store = env
    _w(home / "ok.txt", b"ok")
    real = os.scandir

    class _Bad:
        name = "bad\udc80.txt"

        def __init__(self, folder):
            self.path = str(Path(folder) / "bad_placeholder.txt")

        def stat(self, follow_symlinks=True):
            return os.stat(home / "ok.txt")

        def is_symlink(self):
            return False

        def is_dir(self, follow_symlinks=True):
            return False

        def is_file(self, follow_symlinks=True):
            return True

    class _It:
        def __init__(self, folder):
            self._it = real(folder)
            self._extra = [_Bad(folder)] if Path(folder) == home else []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._it.close()

        def __iter__(self):
            return iter([*self._it, *self._extra])

    def _fake_scandir(folder="."):
        # scan_mod.os 는 전역 os 입니다. 여기서 바꾸면 tempfile/shutil 의
        # 정리(rmtree 는 fd 정수로 scandir 를 부릅니다)까지 가짜를 탑니다.
        # 홈 폴더 말고는 진짜로 넘깁니다.
        if isinstance(folder, (str, os.PathLike)) and Path(folder) == home:
            return _It(folder)
        return real(folder)

    monkeypatch.setattr(scan_mod.os, "scandir", _fake_scandir)
    with caplog.at_level("INFO", logger="gymhall"):
        res = _run(env, FakeUploader())
    assert res.ok and res.uploaded_files == 1 and res.unreadable == 1
    assert {r[0] for r in store.conn.execute("SELECT path FROM files")} == {"ok.txt"}


def test_gymhall_log_lists_at_most_20_folders_per_link(env, caplog):
    home, layout, _ = env
    for i in range(25):
        _w(home / f"f{i:02d}/a.txt")
    with caplog.at_level("INFO", logger="gymhall"):
        _run(env, FakeUploader())
    folder_lines = [r for r in caplog.messages if r.startswith("  f") and r.endswith("1개")]
    assert len(folder_lines) == 20
    assert any("외 5개 폴더" in m for m in caplog.messages)
    text = (layout.logs / CHAIN_LOG).read_text(encoding="utf-8")
    assert all(f"f{i:02d}/" in text for i in range(25)), "체인 로그에는 전부 있어야 합니다"


# ---------------------------------------------------------------------------
# 재검토(2026-11-14)에서 재현된 것들
# ---------------------------------------------------------------------------
def _two_bundles(home):
    _w(home / "a/1.bin", b"a" * (MiB // 2 + 1))
    _w(home / "b/2.bin", b"b" * (MiB // 2 + 1))


def test_disk_full_while_zipping_fails_one_bundle_not_the_run(env, monkeypatch):
    """A: 묶음을 쓰다 디스크가 꽉 차면(OSError) 예전에는 실행 전체가 죽었고,
    이미 올린 묶음은 사슬에 안 붙은 채 매일 같은 자리에서 다시 죽었습니다."""
    import errno

    import gymhall.backup.run as run_mod

    home, _, store = env
    _two_bundles(home)
    real = run_mod.write_bundle

    def full(home_, plan, zpath):
        if zpath.name.endswith("-002.zip"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(home_, plan, zpath)

    monkeypatch.setattr(run_mod, "write_bundle", full)
    up = FakeUploader()
    res = _run(env, up, bundle_mb=1)
    assert res.failed_bundles == 1 and res.seq == 0 and not res.ok
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert [e.path for e in entries_of(m)] == ["a/1.bin"]
    assert _paths(store) == {"a/1.bin"}, "올라간 묶음은 사슬과 상태에 남아야 합니다"


def test_disk_full_while_splitting_fails_one_bundle_not_the_run(env):
    """A: 2GB 를 넘는 묶음은 조각을 쓰느라 원본의 두 배가 필요합니다."""
    import errno

    home, _, store = env
    _two_bundles(home)

    class Full(FakeUploader):
        def upload(self, path, tag, **kw):
            if Path(path).name.endswith("-002.zip"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return super().upload(path, tag, **kw)

    up = Full()
    res = _run(env, up, bundle_mb=1)
    assert res.failed_bundles == 1 and res.seq == 0 and not res.ok
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert [e.path for e in entries_of(m)] == ["a/1.bin"]


def test_an_existing_split_part_name_is_not_overwritten(env):
    """D2: 분할되면 실제 에셋은 name.partNNN 입니다. zip 이름만 보면 지나갑니다."""
    home, _, store = env
    _w(home / "a.txt")
    up = FakeUploader()
    up.assets[("home-2026-11-13", "pc1_home-000000-n1-001.zip.part001")] = b"other pc"
    res = _run(env, up)
    assert res.failed_bundles == 1 and not res.ok
    assert up.assets[("home-2026-11-13", "pc1_home-000000-n1-001.zip.part001")] == b"other pc"


def test_split_bundles_carry_each_part_hash_into_the_chain(env):
    """D2: 정리가 조각마다 GitHub digest 와 맞출 수 있게 사슬에 적습니다."""
    home, _, store = env
    _w(home / "x.bin", os.urandom(200_000))
    up = FakeUploader(split_over=1000)
    _run(env, up)
    m = parse_manifest(up.fetch(_head(store)), expect=_head(store))
    assert m["bundles"][0]["part_sha256"] == ["1" * 64, "2" * 64]
