"""홈 정리(prune) — 지우면 안 되는 경우를 하나씩 막습니다.

실제 홈과 네트워크는 건드리지 않습니다. tmp_path 에 가짜 홈을 만들고,
매니페스트는 chain.build_manifest/canonical_bytes 로 진짜처럼 만들어
가짜 업로더가 내려줍니다.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gymhall.backup import prune
from gymhall.backup.chain import Entry, Link, build_manifest, canonical_bytes, digest
from gymhall.backup.homestate import HomeState
from gymhall.backup.prune import run_prune, safe_rel
from gymhall.paths import Layout

MACHINE = "lab-401"
KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 13, 5, 43, tzinfo=KST)
OLD = "2026-11-10T03:13:00+09:00"
TAG = "home-2026-11-10"


class FakeUploader:
    """ReleaseUploader 의 정리 쪽이 쓰는 부분만 흉내 냅니다."""

    def __init__(self) -> None:
        self.releases: dict[str, dict] = {}
        self.blobs: dict[int, bytes] = {}
        self.downloads: list[str] = []
        self._next = 1

    def add_asset(self, tag: str, name: str, data: bytes | None = None,
                  size: int | None = None, sha256: str | None = None) -> dict:
        """GitHub 처럼 digest("sha256:...") 를 붙입니다. 내용이 없으면 sha256 인자로."""
        rel = self.releases.setdefault(
            tag, {"id": 100 + len(self.releases), "tag_name": tag, "assets": []})
        asset = {"id": self._next, "name": name,
                 "size": len(data) if data is not None else size}
        dg = hashlib.sha256(data).hexdigest() if data is not None else sha256
        if dg:
            asset["digest"] = f"sha256:{dg}"
        self._next += 1
        rel["assets"].append(asset)
        if data is not None:
            self.blobs[asset["id"]] = data
        return asset

    def remove_asset(self, tag: str, name: str) -> None:
        rel = self.releases[tag]
        rel["assets"] = [a for a in rel["assets"] if a["name"] != name]

    def blob_of(self, tag: str, name: str) -> int:
        return next(a["id"] for a in self.releases[tag]["assets"] if a["name"] == name)

    # -- 실제 API 모양 --
    def get_release(self, tag):
        return self.releases.get(tag)

    def list_assets(self, release, *, refresh=False):
        return list(release["assets"])

    def find_asset(self, tag, name):
        rel = self.get_release(tag)
        if rel is None:
            return None
        return next((a for a in self.list_assets(rel) if a["name"] == name), None)

    def download_asset(self, asset):
        self.downloads.append(asset["name"])
        return self.blobs[asset["id"]]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Chain:
    """가짜 GitHub 에 사슬을 올립니다(업로드 쪽 흉내)."""

    def __init__(self, up: FakeUploader, home: Path) -> None:
        self.up = up
        self.home = home
        self.head: Link | None = None
        self.seq = -1

    def publish(self, rels: list[str] | None = None, *, entries: list[Entry] | None = None,
                tag: str = TAG, created_at: str = OLD, machine: str = MACHINE,
                upload_bundle: bool = True, bundle_size: int | None = None,
                parts: int = 0, seq: int | None = None, home: str | None = None,
                bundle_sha: str | None = None) -> dict:
        seq = self.seq + 1 if seq is None else seq
        bname = f"{machine}-home-{seq:06d}-000.zip"
        if entries is None:
            entries = []
            for r in rels or []:
                data = (self.home / r).read_bytes()
                entries.append(Entry(r, len(data), 0, sha(data), bname, r))
        zsize = 1000 + seq
        zsha = sha(bname.encode()) if bundle_sha is None else bundle_sha  # 가짜 zip 해시
        if upload_bundle:
            if parts:
                info = {"original_name": bname, "original_size": zsize, "sha256": zsha,
                        "part_count": parts, "part_prefix": bname}
                self.up.add_asset(tag, f"{bname}.parts.json", json.dumps(info).encode())
                each = zsize // parts
                for i in range(1, parts + 1):
                    sz = each if i < parts else zsize - each * (parts - 1)
                    self.up.add_asset(tag, f"{bname}.part{i:03d}", size=sz,
                                      sha256=sha(f"{bname}{i}".encode()))
            else:
                self.up.add_asset(tag, bname, size=zsize if bundle_size is None else bundle_size,
                                  sha256=zsha)
        m = build_manifest(
            machine=machine, seq=seq, prev=self.head, tag=tag,
            home=str(self.home.resolve()) if home is None else home,
            created_at=created_at,
            bundles=[{"asset": bname, "size": zsize, "sha256": zsha, "parts": parts}],
            entries=entries)
        data = canonical_bytes(m)
        mname = f"{machine}-home-chain-{seq:06d}.json"
        self.up.add_asset(tag, mname, data)
        self.head = Link(tag, mname, digest(data))
        self.seq = seq
        return m

    def anchor(self, state: HomeState, *, seq: int | None = None) -> None:
        """업로드 쪽이 매니페스트를 올린 뒤 하는 일: head 를 적고 commit."""
        state.set_head(self.head, self.seq if seq is None else seq)
        state.commit()


@pytest.fixture
def env(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    layout = Layout(tmp_path / "data")
    up = FakeUploader()
    with HomeState.open(layout.state / "home_backup.sqlite") as state:
        yield home, layout, state, up, Chain(up, home)


def pending(state: HomeState) -> set[str]:
    return {r.path for r in state.iter_pending()}


def put(home: Path, rel: str, data: bytes = b"hello") -> Path:
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def go(env, *, dry_run: bool = False, now: datetime = NOW, protect=(), min_age: float = 24):
    home, layout, state, up, _ = env
    return run_prune(home=home, uploader=up, state=state, layout=layout, machine=MACHINE,
                     min_age_hours=min_age, protect=list(protect), dry_run=dry_run, now=now)


# ---------------------------------------------------------------------------
# 정상 흐름
# ---------------------------------------------------------------------------
def test_happy_path_deletes_verified_files_and_keeps_directories(env):
    home, layout, state, up, chain = env
    a = put(home, "Documents/과제/a.hwp", b"aaaa")
    b = put(home, "Documents/b.txt", b"bb")
    c = put(home, "top.txt", b"c")
    chain.publish(["Documents/과제/a.hwp", "Documents/b.txt", "top.txt"])
    chain.anchor(state)

    res = go(env)

    assert res.chain_ok and res.errors == 0
    assert res.deleted == 3 and res.bytes_freed == 7
    assert not a.exists() and not b.exists() and not c.exists()
    assert (home / "Documents" / "과제").is_dir()          # 디렉터리는 남깁니다
    assert pending(state) == set()
    assert state.verified() == {chain.head.sha256}
    text = (layout.logs / "home-prune.log").read_text(encoding="utf-8")
    assert "Documents/과제/" in text and "a.hwp" in text and "(홈)/" in text
    # 디스크에도 저장됐습니다
    with HomeState.open(layout.state / "home_backup.sqlite") as again:
        assert again.verified() == {chain.head.sha256}
        assert again.pending_count() == 0


def test_no_head_means_nothing_happens(env):
    home, layout, state, up, _ = env
    f = put(home, "a.txt")
    res = go(env)
    assert res.chain_ok and res.deleted == 0
    assert f.exists() and up.downloads == []
    assert not (layout.logs / "home-prune.log").exists()


# ---------------------------------------------------------------------------
# 사슬이 틀리면 하나도 지우지 않습니다
# ---------------------------------------------------------------------------
def _assert_untouched(res, state, *files):
    assert res.chain_ok is False
    assert res.deleted == 0
    for f in files:
        assert f.exists()
    assert state.verified() == set()
    assert state.pending_count() == 0


def test_tampered_head_manifest_bytes_delete_nothing(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    aid = up.blob_of(TAG, chain.head.asset)
    up.blobs[aid] = up.blobs[aid].replace(b'"seq":0', b'"seq":0 ')
    _assert_untouched(go(env), state, f)


def test_tampered_older_link_blocks_newer_files_too(env):
    home, layout, state, up, chain = env
    f0 = put(home, "old.txt", b"0")
    chain.publish(["old.txt"])
    first = chain.head
    f1 = put(home, "new.txt", b"1")
    chain.publish(["new.txt"])
    chain.anchor(state)
    aid = up.blob_of(TAG, first.asset)
    up.blobs[aid] = up.blobs[aid].replace(b"old.txt", b"xld.txt")
    _assert_untouched(go(env), state, f0, f1)


def test_broken_seq_deletes_nothing(env):
    home, layout, state, up, chain = env
    f0 = put(home, "a.txt", b"0")
    chain.publish(["a.txt"])
    f1 = put(home, "b.txt", b"1")
    chain.publish(["b.txt"], seq=5)        # 1..4 가 빠졌습니다
    chain.anchor(state)
    _assert_untouched(go(env), state, f0, f1)


def test_head_seq_disagreeing_with_local_state_deletes_nothing(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state, seq=3)
    _assert_untouched(go(env), state, f)


def test_chain_of_another_home_deletes_nothing(env, tmp_path):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"], home=str(tmp_path / "other-home"))
    chain.anchor(state)
    _assert_untouched(go(env), state, f)


def test_chain_home_compares_case_and_separators_like_windows(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    h = str(home.resolve())
    if os.name == "nt":
        h = h.upper().replace("\\", "/") + "/"
    chain.publish(["a.txt"], home=h)
    chain.anchor(state)
    assert go(env).deleted == 1 and not f.exists()


def test_foreign_machine_chain_deletes_nothing(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"], machine="other-pc")
    chain.anchor(state)
    _assert_untouched(go(env), state, f)


def test_manifest_missing_on_github_deletes_nothing(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    up.remove_asset(TAG, chain.head.asset)
    _assert_untouched(go(env), state, f)


def test_download_error_deletes_nothing(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)

    def boom(asset):
        raise ConnectionError("끊김")

    up.download_asset = boom
    _assert_untouched(go(env), state, f)


# ---------------------------------------------------------------------------
# 원격 묶음
# ---------------------------------------------------------------------------
def test_missing_remote_bundle_keeps_file_pending(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"], upload_bundle=False)
    chain.anchor(state)
    res = go(env)
    assert res.chain_ok and res.missing_remote == 1 and res.deleted == 0
    assert f.exists()
    assert "a.txt" in pending(state)


def test_remote_bundle_size_mismatch_keeps_file(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"], bundle_size=999)
    chain.anchor(state)
    res = go(env)
    assert res.missing_remote == 1 and res.deleted == 0 and f.exists()


def test_remote_bundle_digest_mismatch_keeps_file(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    for a in up.releases[TAG]["assets"]:
        if a["name"].endswith(".zip"):
            a["digest"] = "sha256:" + "1" * 64
    res = go(env)
    assert res.missing_remote == 1 and f.exists()


def test_split_bundle_needs_every_part(env):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    bname = f"{MACHINE}-home-000000-000.zip"
    up.remove_asset(TAG, f"{bname}.part002")
    res = go(env)
    assert res.missing_remote == 1 and f.exists()

    up.add_asset(TAG, f"{bname}.part002", size=500,      # 1000 = 500 + 500
                 sha256="2" * 64)
    res = go(env)
    assert res.deleted == 1 and not f.exists()


def test_split_bundle_parts_must_add_up_to_bundle_size(env):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    for a in up.releases[TAG]["assets"]:
        if a["name"].endswith(".part002"):
            a["size"] = 1                                  # 잘린 조각
    assert go(env).missing_remote == 1 and f.exists()


def test_unsplit_bundle_without_digest_is_not_trusted(env):
    """GitHub digest 가 없으면 크기만 맞아도 믿지 않습니다."""
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    for a in up.releases[TAG]["assets"]:
        if a["name"].endswith(".zip"):
            del a["digest"]
    res = go(env)
    assert res.missing_remote == 1 and res.deleted == 0 and f.exists()
    assert "a.txt" in pending(state)


def _split_info(up: FakeUploader) -> tuple[dict, int]:
    name = f"{MACHINE}-home-000000-000.zip.parts.json"
    aid = up.blob_of(TAG, name)
    return json.loads(up.blobs[aid]), aid


@pytest.mark.parametrize("field,value", [
    ("sha256", "9" * 64),                                # 다른 묶음으로 바뀜
    ("part_count", 3),
    ("part_prefix", "other.zip"),
    ("original_size", 1),
])
def test_split_bundle_parts_json_must_match_the_manifest(env, field, value):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    info, aid = _split_info(up)
    info[field] = value
    up.blobs[aid] = json.dumps(info).encode()
    res = go(env)
    assert res.missing_remote == 1 and res.deleted == 0 and f.exists()


def test_split_bundle_replaced_after_verification_is_not_deleted(env):
    """재현된 사고: 확인 뒤 같은 이름의 다른 분할 묶음이 올라오면 지우면 안 됩니다."""
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    go(env, min_age=10_000)                     # 확인만
    bname = f"{MACHINE}-home-000000-000.zip"
    for suffix in (".parts.json", ".part001", ".part002"):
        up.remove_asset(TAG, bname + suffix)
    other = {"original_name": bname, "original_size": 1000, "sha256": "8" * 64,
             "part_count": 2, "part_prefix": bname}
    up.add_asset(TAG, f"{bname}.parts.json", json.dumps(other).encode())
    up.add_asset(TAG, f"{bname}.part001", size=500, sha256="a" * 64)
    up.add_asset(TAG, f"{bname}.part002", size=500, sha256="b" * 64)
    res = go(env)
    assert res.missing_remote == 1 and res.deleted == 0 and f.exists()


def test_split_bundle_without_a_sha256_in_the_manifest_is_not_trusted(env):
    """매니페스트에 묶음 해시가 비어 있으면 parts.json 도 빈 값과 '같아' 보입니다."""
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2, bundle_sha="")
    chain.anchor(state)
    assert go(env).missing_remote == 1 and f.exists()


def test_split_part_without_digest_is_not_trusted(env):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    for a in up.releases[TAG]["assets"]:
        if a["name"].endswith(".part001"):
            del a["digest"]
    assert go(env).missing_remote == 1 and f.exists()


@pytest.mark.parametrize("blob", [b"not json", b"[1, 2]", b"\xff\xfe"])
def test_split_bundle_with_unreadable_parts_json_keeps_file(env, blob):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    _, aid = _split_info(up)
    up.blobs[aid] = blob
    assert go(env).missing_remote == 1 and f.exists()


def test_split_parts_json_download_error_keeps_file(env):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    real = up.download_asset

    def flaky(asset):
        if asset["name"].endswith(".parts.json"):
            raise ConnectionError("끊김")
        return real(asset)

    up.download_asset = flaky
    assert go(env).missing_remote == 1 and f.exists()


def test_bundle_check_is_cached_per_bundle(env):
    home, layout, state, up, chain = env
    names = [f"d/{i}.bin" for i in range(5)]
    for n in names:
        put(home, n, n.encode())
    chain.publish(names, parts=2)
    chain.anchor(state)
    up.downloads.clear()
    res = go(env)
    assert res.deleted == 5
    assert [d for d in up.downloads if d.endswith(".parts.json")] == [
        f"{MACHINE}-home-000000-000.zip.parts.json"]


def test_split_bundle_without_parts_json_keeps_file(env):
    home, layout, state, up, chain = env
    f = put(home, "big.bin", b"x" * 10)
    chain.publish(["big.bin"], parts=2)
    chain.anchor(state)
    up.remove_asset(TAG, f"{MACHINE}-home-000000-000.zip.parts.json")
    assert go(env).missing_remote == 1 and f.exists()


# ---------------------------------------------------------------------------
# 로컬 파일 쪽
# ---------------------------------------------------------------------------
def test_same_size_different_content_is_changed_not_deleted(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt", b"hello")
    chain.publish(["a.txt"])
    chain.anchor(state)
    f.write_bytes(b"HELLO")
    res = go(env)
    assert res.changed == 1 and res.deleted == 0
    assert f.read_bytes() == b"HELLO"
    assert "a.txt" not in pending(state)


def test_different_size_is_changed_without_hashing(env, monkeypatch):
    home, layout, state, up, chain = env
    f = put(home, "a.txt", b"hello")
    chain.publish(["a.txt"])
    chain.anchor(state)
    f.write_bytes(b"hello world")

    def no_hash(path):
        raise AssertionError("크기가 다르면 해시를 계산할 필요가 없습니다")

    monkeypatch.setattr(prune, "sha256_file", no_hash)
    res = go(env)
    assert res.changed == 1 and f.exists()


def test_gone_file_is_dropped_from_pending(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    f.unlink()
    res = go(env)
    assert res.gone == 1 and pending(state) == set()


def test_too_new_files_stay(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"], created_at=(NOW - timedelta(hours=1)).isoformat())
    chain.anchor(state)
    res = go(env)
    assert res.too_new == 1 and res.deleted == 0 and f.exists()
    assert "a.txt" in pending(state)


def test_protected_path_is_kept(env):
    home, layout, state, up, chain = env
    repo_file = put(home, "Desktop/GYM-W-HALL-2F/src/x.py", b"code")
    other = put(home, "Desktop/notes.txt", b"n")
    chain.publish(["Desktop/GYM-W-HALL-2F/src/x.py", "Desktop/notes.txt"])
    chain.anchor(state)
    guard = home / "Desktop" / "GYM-W-HALL-2F"
    if os.name == "nt":       # 윈도우는 대소문자를 가리지 않습니다
        guard = home / "desktop" / "gym-w-hall-2f"
    res = go(env, protect=[guard])
    assert res.protected == 1 and repo_file.exists()
    assert res.deleted == 1 and not other.exists()
    assert pending(state) == set()          # 보호 항목은 대기 목록에서 뺍니다


def test_protecting_the_exact_file_keeps_it(env):
    home, layout, state, up, chain = env
    cfg = put(home, "config.toml", b"x=1")
    chain.publish(["config.toml"])
    chain.anchor(state)
    res = go(env, protect=[cfg])
    assert res.protected == 1 and cfg.exists()


def test_data_dir_inside_home_is_always_protected(tmp_path):
    home = tmp_path / "home"
    layout = Layout(home / "gymhall-data")
    up = FakeUploader()
    chain = Chain(up, home)
    f = put(home, "gymhall-data/logs/gymhall.log", b"log")
    chain.publish(["gymhall-data/logs/gymhall.log"])
    with HomeState.open(tmp_path / "s.sqlite") as state:
        chain.anchor(state)
        res = run_prune(home=home, uploader=up, state=state, layout=layout, machine=MACHINE,
                        min_age_hours=24, protect=[], dry_run=False, now=NOW)
    assert res.protected == 1 and f.exists()


@pytest.mark.parametrize("bad", [
    "../outside.txt",
    "a/../../outside.txt",
    "/etc/passwd",
    "C:/Windows/x.txt",
    "C:x.txt",
    "a\\..\\..\\outside.txt",
    "./a.txt",
    "a//b.txt",
    "",
])
def test_safe_rel_rejects(bad):
    assert safe_rel(bad) is None


def test_safe_rel_accepts_plain_relative_paths():
    assert str(safe_rel("Documents/과제/a b.hwp")) == "Documents/과제/a b.hwp"
    assert str(safe_rel(".git/config")) == ".git/config"


@pytest.mark.parametrize("make_path", [
    lambda out: "../outside.txt",
    lambda out: str(out),                       # 절대 경로
    lambda out: out.as_posix(),
])
def test_entry_escaping_home_is_never_deleted(env, tmp_path, make_path):
    home, layout, state, up, chain = env
    out = tmp_path / "outside.txt"
    out.write_bytes(b"secret")
    rel = make_path(out)
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry(rel, 6, 0, sha(b"secret"), bname, "x")])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and res.errors == 1
    assert out.exists()


def test_symlink_escaping_home_is_never_followed(env, tmp_path):
    home, layout, state, up, chain = env
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "f.txt"
    target.write_bytes(b"keep")
    try:
        (home / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경은 심볼릭 링크를 만들 수 없습니다")
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry("link/f.txt", 4, 0, sha(b"keep"), bname, "link/f.txt")])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and target.exists()


def test_symlink_escape_is_caught_even_if_reparse_detection_misses(env, tmp_path,
                                                                   monkeypatch):
    """리parse 판별이 놓쳐도 resolve 후 '홈 안인가' 가 한 번 더 막습니다."""
    home, layout, state, up, chain = env
    outside = tmp_path / "elsewhere2"
    outside.mkdir()
    target = outside / "f.txt"
    target.write_bytes(b"keep")
    try:
        (home / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경은 심볼릭 링크를 만들 수 없습니다")
    monkeypatch.setattr(prune, "_is_reparse", lambda p: False)
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry("link/f.txt", 4, 0, sha(b"keep"), bname, "link/f.txt")])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and res.errors == 1 and target.exists()


def test_inside_compares_whole_components():
    base = Path("/x/home")
    assert prune._inside(Path("/x/home"), base)
    assert prune._inside(Path("/x/home/a/b"), base)
    assert not prune._inside(Path("/x/home2/a"), base)
    assert not prune._inside(Path("/x"), base)


@pytest.mark.skipif(os.name != "nt", reason="윈도우만 대소문자를 가리지 않습니다")
def test_inside_ignores_case_on_windows():
    assert prune._inside(Path(r"C:\Users\Me\Desktop\Repo\x.py"),
                         Path(r"c:\users\me\desktop\repo"))


def test_alias_name_resolving_to_another_file_is_never_deleted(env, monkeypatch):
    """항목 이름과 실제로 열리는 파일 이름이 다르면 그 파일이 아닙니다."""
    home, layout, state, up, chain = env
    real_file = put(home, "realdir/x.txt", b"same")
    try:
        (home / "aliasdir").symlink_to(home / "realdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경은 심볼릭 링크를 만들 수 없습니다")
    monkeypatch.setattr(prune, "_is_reparse", lambda p: False)   # 판별이 놓쳤다고 치고
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry("aliasdir/x.txt", 4, 0, sha(b"same"), bname, "x")])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and res.errors == 1 and real_file.exists()
    assert pending(state) == set()


@pytest.mark.skipif(os.name != "nt", reason="Win32 이름 별칭은 윈도우에만 있습니다")
@pytest.mark.parametrize("alias", ["a.", "a ", "a. ", "a.."])
def test_win32_trailing_dot_or_space_alias_is_never_deleted(env, alias):
    home, layout, state, up, chain = env
    f = put(home, "a", b"same")
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry(alias, 4, 0, sha(b"same"), bname, alias)])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and res.errors == 1 and f.exists()


@pytest.mark.skipif(os.name != "nt", reason="8.3 짧은 이름은 윈도우에만 있습니다")
def test_win32_short_name_alias_is_never_deleted(env):
    import ctypes

    home, layout, state, up, chain = env
    f = put(home, "LongDirectoryName/f.txt", b"same")
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(f.parent), buf, 1024)
    short = Path(buf.value).name
    if not short or short == "LongDirectoryName":
        pytest.skip("이 볼륨은 8.3 이름을 만들지 않습니다")
    rel = f"{short}/f.txt"
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry(rel, 4, 0, sha(b"same"), bname, rel)])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 0 and res.errors == 1 and f.exists()


@pytest.mark.skipif(os.name != "nt", reason="윈도우만 대소문자를 가리지 않습니다")
def test_case_only_difference_is_the_same_file_and_logs_the_real_name(env):
    home, layout, state, up, chain = env
    f = put(home, "Docs/b.txt", b"same")
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry("DOCS/B.TXT", 4, 0, sha(b"same"), bname, "DOCS/B.TXT")])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 1 and not f.exists()
    assert res.paths == ["Docs/b.txt"]                   # 실제로 지운 이름


def test_reparse_parent_is_never_followed(env, monkeypatch):
    home, layout, state, up, chain = env
    f = put(home, "Junction/inner/a.txt")
    g = put(home, "Normal/b.txt")
    chain.publish(["Junction/inner/a.txt", "Normal/b.txt"])
    chain.anchor(state)
    real = prune._is_reparse
    monkeypatch.setattr(prune, "_is_reparse",
                        lambda p: p.name == "Junction" or real(p))
    res = go(env)
    assert f.exists() and res.protected == 1
    assert not g.exists() and res.deleted == 1
    assert pending(state) == set()          # 리parse 를 거치는 항목도 뺍니다


def test_reparse_file_itself_is_never_deleted(env, monkeypatch):
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    chain.anchor(state)
    monkeypatch.setattr(prune, "_is_reparse", lambda p: p.name == "a.txt")
    res = go(env)
    assert f.exists() and res.deleted == 0


def test_directory_entry_is_never_removed(env):
    home, layout, state, up, chain = env
    d = home / "Documents"
    d.mkdir()
    bname = f"{MACHINE}-home-000000-000.zip"
    chain.publish(entries=[Entry("Documents", 0, 0, sha(b""), bname, "Documents")])
    chain.anchor(state)
    res = go(env)
    assert d.is_dir() and res.deleted == 0
    assert res.protected == 1 and res.errors == 0


# ---------------------------------------------------------------------------
# 대기 목록
# ---------------------------------------------------------------------------
def test_newest_entry_for_a_path_wins(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt", b"v1")
    chain.publish(["a.txt"])
    f.write_bytes(b"v2-longer")
    chain.publish(["a.txt"])
    chain.anchor(state)
    res = go(env)
    assert res.deleted == 1 and res.changed == 0 and not f.exists()


def test_pending_rows_carry_their_manifest_seq(env):
    """state 는 seq 로 새것을 고릅니다. seq 가 틀리면 순서에 기대게 됩니다."""
    home, layout, state, up, chain = env
    put(home, "a.txt", b"v1")
    put(home, "b.txt", b"b")
    chain.publish(["a.txt", "b.txt"])
    put(home, "a.txt", b"v2")
    chain.publish(["a.txt"], tag="home-2026-11-11")
    chain.anchor(state)
    go(env, min_age=10_000)
    rows = {r.path: r for r in state.iter_pending()}
    assert (rows["a.txt"].seq, rows["a.txt"].tag) == (1, "home-2026-11-11")
    assert (rows["b.txt"].seq, rows["b.txt"].tag) == (0, TAG)
    assert rows["a.txt"].created_at == OLD and rows["a.txt"].bundle_size == 1001


def test_older_content_does_not_match_newest_entry(env):
    home, layout, state, up, chain = env
    f = put(home, "a.txt", b"v1")
    chain.publish(["a.txt"])
    f.write_bytes(b"v2-longer")
    chain.publish(["a.txt"])
    chain.anchor(state)
    f.write_bytes(b"v1")          # 사슬의 최신 항목과 다릅니다
    res = go(env)
    assert res.changed == 1 and f.exists()


def test_incremental_run_still_prunes_earlier_pending(env):
    home, layout, state, up, chain = env
    a = put(home, "a.txt", b"a")
    chain.publish(["a.txt"], created_at="2026-11-13T04:00:00+09:00")
    chain.anchor(state)
    res = go(env)                                        # 아직 1시간 남짓
    assert res.too_new == 1 and a.exists()
    first_hash = chain.head.sha256
    assert state.verified() == {first_hash}

    b = put(home, "b.txt", b"b")
    chain.publish(["b.txt"], tag="home-2026-11-14", created_at="2026-11-14T04:00:00+09:00")
    chain.anchor(state)
    up.downloads.clear()
    res = go(env, now=datetime(2026, 11, 16, 5, 43, tzinfo=KST))
    # 이미 확인한 고리는 다시 받지 않습니다
    assert up.downloads == [chain.head.asset]
    assert res.deleted == 2 and not a.exists() and not b.exists()
    assert state.verified() == {first_hash, chain.head.sha256}


def test_already_verified_head_uses_pending_only(env):
    home, layout, state, up, chain = env
    a = put(home, "a.txt", b"a")
    chain.publish(["a.txt"])
    chain.anchor(state)
    go(env, min_age=10_000)                     # 확인만 하고, 너무 최근이라 남김
    up.downloads.clear()
    res = go(env)
    assert up.downloads == [] and res.deleted == 1 and not a.exists()


def _db_content(path: Path) -> tuple:
    with HomeState.open(path) as st:
        rows = tuple(st.iter_pending())
        return st.get_head(), frozenset(st.verified()), rows


def test_dry_run_deletes_nothing_and_persists_nothing(env):
    home, layout, state, up, chain = env
    a = put(home, "a.txt", b"a")
    chain.publish(["a.txt"])
    chain.anchor(state)
    db = layout.state / "home_backup.sqlite"
    before = _db_content(db)
    res = go(env, dry_run=True)
    assert res.chain_ok and res.deleted == 0 and res.would_delete == 1
    assert res.paths == ["a.txt"]
    assert a.exists()
    assert state.verified() == set() and state.pending_count() == 0
    assert _db_content(db) == before
    assert not (layout.logs / "home-prune.log").exists()


def test_dry_run_after_real_run_changes_nothing(env):
    home, layout, state, up, chain = env
    a = put(home, "a.txt", b"a")
    b = put(home, "b.txt", b"b")
    chain.publish(["a.txt"])
    chain.anchor(state)
    go(env, min_age=10_000)                     # 확인·commit 만
    chain.publish(["b.txt"])
    chain.anchor(state)
    db = layout.state / "home_backup.sqlite"
    before = _db_content(db)
    res = go(env, dry_run=True, now=NOW + timedelta(days=30))
    assert res.would_delete == 2 and a.exists() and b.exists()
    assert _db_content(db) == before
    assert state.pending_count() == 1 and len(state.verified()) == 1


# ---------------------------------------------------------------------------
# 트랜잭션
# ---------------------------------------------------------------------------
def test_chain_failure_leaves_db_unchanged(env):
    home, layout, state, up, chain = env
    a = put(home, "a.txt", b"a")
    chain.publish(["a.txt"])
    chain.anchor(state)
    go(env, min_age=10_000)                     # 고리 0 확인, a 는 대기
    put(home, "b.txt", b"b")
    chain.publish(["b.txt"])
    chain.anchor(state)
    aid = up.blob_of(TAG, chain.head.asset)
    up.blobs[aid] += b" "                       # 새 고리 변조
    db = layout.state / "home_backup.sqlite"
    before = _db_content(db)
    res = go(env, now=NOW + timedelta(days=30))
    assert res.chain_ok is False and res.deleted == 0 and a.exists()
    assert _db_content(db) == before            # 다시 열어 봐도 그대로


def test_malformed_entry_in_a_verified_hash_is_still_a_chain_failure(env):
    """해시가 맞아도 항목 형식이 틀리면 사슬 실패. 앞 매니페스트 행도 남지 않습니다."""
    home, layout, state, up, chain = env
    f = put(home, "a.txt")
    chain.publish(["a.txt"])
    m = build_manifest(machine=MACHINE, seq=1, prev=chain.head, tag=TAG,
                       home=str(home.resolve()),
                       created_at=OLD, bundles=[], entries=[])
    m["entries"] = [{"path": "x", "bogus": 1}]
    data = canonical_bytes(m)
    up.add_asset(TAG, "bad.json", data)
    chain.head, chain.seq = Link(TAG, "bad.json", digest(data)), 1
    chain.anchor(state)
    _assert_untouched(go(env), state, f)


def test_verification_is_committed_before_deleting(env, monkeypatch):
    home, layout, state, up, chain = env
    put(home, "a.txt", b"a")
    chain.publish(["a.txt"])
    chain.anchor(state)

    def crash(path):
        raise RuntimeError("전원 꺼짐")

    monkeypatch.setattr(prune, "_unlink", crash)
    with pytest.raises(RuntimeError):
        go(env)
    state.rollback()                            # 프로세스가 죽은 것처럼
    with HomeState.open(layout.state / "home_backup.sqlite") as again:
        assert again.verified() == {chain.head.sha256}
        assert {r.path for r in again.iter_pending()} == {"a.txt"}


def test_unlink_errors_are_reported_and_rows_kept(env, monkeypatch):
    home, layout, state, up, chain = env
    names = [f"d/{i:02d}.txt" for i in range(6)]
    for n in names:
        put(home, n, n.encode())
    chain.publish(names)
    chain.anchor(state)
    real = prune._unlink
    calls: list[Path] = []

    def flaky(path):
        calls.append(path)
        if len(calls) > 2:
            raise PermissionError("잠김")
        real(path)

    monkeypatch.setattr(prune, "_unlink", flaky)
    res = go(env)
    assert res.deleted == 2 and res.errors == 4     # 조용한 성공이 아닙니다
    assert pending(state) == set(names[2:])

    monkeypatch.setattr(prune, "_unlink", real)
    res = go(env)
    assert res.deleted == 4 and res.errors == 0 and pending(state) == set()


def test_crash_mid_deletion_is_harmless_next_run(env, monkeypatch):
    home, layout, state, up, chain = env
    names = [f"d/{i:02d}.txt" for i in range(7)]
    for n in names:
        put(home, n, n.encode())
    chain.publish(names)
    chain.anchor(state)
    monkeypatch.setattr(prune, "COMMIT_EVERY", 2)
    real = prune._unlink
    unlinked: list[str] = []

    def dies_after_three(path):
        if len(unlinked) == 3:
            raise KeyboardInterrupt
        real(path)
        unlinked.append(path.name)

    monkeypatch.setattr(prune, "_unlink", dies_after_three)
    with pytest.raises(KeyboardInterrupt):
        go(env)
    state.rollback()                            # 죽은 프로세스: commit 안 된 것은 사라짐
    # 2건은 commit 됐고, 3번째는 지웠지만 행이 남았습니다
    assert pending(state) == set(names[2:])
    text = (layout.logs / "home-prune.log").read_text(encoding="utf-8")
    assert "02.txt" in text                     # 죽기 전에 지운 것도 기록됩니다

    monkeypatch.setattr(prune, "_unlink", lambda p: (unlinked.append(p.name), real(p)))
    res = go(env)
    assert res.gone == 1 and res.deleted == 4 and res.errors == 0
    assert len(unlinked) == len(set(unlinked)) == 7     # 두 번 지운 것은 없습니다
    assert pending(state) == set()
    assert all(not (home / n).exists() for n in names)
