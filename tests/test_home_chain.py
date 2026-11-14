"""앵커 체인: 고리 하나라도 이상하면 사슬 전체를 믿지 않습니다."""

from __future__ import annotations

import json

import pytest

from gymhall.backup import chain as C


def _entry(path="Documents/a.txt"):
    return C.Entry(path=path, size=3, mtime_ns=1, sha256="a" * 64,
                   bundle="pc_home-000000-000.zip", member=path)


def _make(n, machine="pc"):
    """n 개짜리 사슬. (head, {sha: bytes}) 를 돌려줍니다."""
    store: dict[str, bytes] = {}
    prev = None
    for seq in range(n):
        tag = f"home-2026-11-{13 + seq:02d}"
        m = C.build_manifest(machine=machine, seq=seq, prev=prev, tag=tag,
                             home="C:/Users/x", created_at="2026-11-13T03:13:00+09:00",
                             bundles=[{"asset": f"b{seq}.zip", "size": 1,
                                       "sha256": "b" * 64, "parts": 0}],
                             entries=[_entry(f"d/{seq}.txt")])
        data = C.canonical_bytes(m)
        prev = C.Link(tag, f"pc_home-chain-{seq:06d}.json", C.digest(data))
        store[prev.sha256] = data
    return prev, store


def _fetch(store):
    return lambda link: store[link.sha256]


def test_walk_returns_newest_first_back_to_genesis():
    head, store = _make(3)
    got = C.walk_chain(head, _fetch(store), machine="pc")
    assert [m["seq"] for m in got] == [2, 1, 0]
    assert C.entries_of(got[0])[0].path == "d/2.txt"


def test_tampered_bytes_break_the_chain():
    head, store = _make(3)
    first = C.walk_chain(head, _fetch(store), machine="pc")[-1]
    sha0 = C.Link.from_dict(C.walk_chain(head, _fetch(store), machine="pc")[1]["prev"]).sha256
    first["entries"][0]["path"] = "Windows/System32/x.dll"
    store[sha0] = C.canonical_bytes(first)
    with pytest.raises(C.ChainError, match="해시 불일치"):
        C.walk_chain(head, _fetch(store), machine="pc")


def test_foreign_machine_is_rejected():
    head, store = _make(2, machine="other")
    with pytest.raises(C.ChainError, match="다른 PC"):
        C.walk_chain(head, _fetch(store), machine="pc")


def test_seq_gap_is_rejected():
    head, store = _make(2)
    data = store[head.sha256]
    m = json.loads(data)
    m["seq"] = 5
    forged = C.canonical_bytes(m)
    new_head = C.Link(head.tag, head.asset, C.digest(forged))
    store[new_head.sha256] = forged
    with pytest.raises(C.ChainError, match="seq"):
        # head 는 5, 앞 고리는 0 → 4 를 기대
        C.walk_chain(new_head, _fetch(store), machine="pc")


def test_nonzero_seq_without_prev_is_rejected():
    m = C.build_manifest(machine="pc", seq=0, prev=None, tag="t", home="h",
                         created_at="x", bundles=[], entries=[])
    m["seq"] = 3
    data = C.canonical_bytes(m)
    link = C.Link("t", "a", C.digest(data))
    with pytest.raises(C.ChainError, match="앞 고리"):
        C.walk_chain(link, lambda _l: data, machine="pc")


def test_tag_must_match_location():
    """다른 릴리스에 있는 매니페스트를 이 위치의 것으로 속일 수 없습니다."""
    head, store = _make(1)
    moved = C.Link("home-2099-01-01", head.asset, head.sha256)
    with pytest.raises(C.ChainError, match="tag"):
        C.walk_chain(moved, _fetch(store), machine="pc")


def test_stop_at_skips_already_verified_part():
    head, store = _make(3)
    all_ = C.walk_chain(head, _fetch(store), machine="pc")
    verified_seq1 = C.Link.from_dict(all_[0]["prev"]).sha256
    calls = []

    def fetch(link):
        calls.append(link.sha256)
        return store[link.sha256]

    got = C.walk_chain(head, fetch, machine="pc", stop_at=[verified_seq1])
    assert [m["seq"] for m in got] == [2]
    assert calls == [head.sha256]


def test_build_rejects_genesis_with_prev_and_later_without():
    link = C.Link("t", "a", "c" * 64)
    with pytest.raises(ValueError):
        C.build_manifest(machine="pc", seq=0, prev=link, tag="t", home="h",
                         created_at="x", bundles=[], entries=[])
    with pytest.raises(ValueError):
        C.build_manifest(machine="pc", seq=1, prev=None, tag="t", home="h",
                         created_at="x", bundles=[], entries=[])


def test_canonical_bytes_are_stable_and_utf8():
    m = {"b": "과제", "a": 1}
    assert C.canonical_bytes(m) == '{"a":1,"b":"과제"}'.encode()


def test_bad_link_dict_raises_chain_error():
    with pytest.raises(C.ChainError):
        C.Link.from_dict({"tag": "t", "asset": "a"})
    with pytest.raises(C.ChainError):
        C.Link.from_dict({"tag": "t", "asset": "a", "sha256": "short"})


def test_visit_streams_without_keeping_manifests():
    head, store = _make(3)
    seen = []
    out = C.walk_chain(head, _fetch(store), machine="pc", visit=lambda m: seen.append(m["seq"]))
    assert out == [] and seen == [2, 1, 0]


def test_visit_still_raises_on_a_broken_older_link():
    """앞 고리들을 visit 한 뒤에 끊겨도 ChainError — 호출자가 되돌립니다."""
    head, store = _make(3)
    sha0 = C.Link.from_dict(
        C.walk_chain(head, _fetch(store), machine="pc")[1]["prev"]).sha256
    store[sha0] = b"{}"
    seen = []
    with pytest.raises(C.ChainError):
        C.walk_chain(head, _fetch(store), machine="pc", visit=lambda m: seen.append(m["seq"]))
    assert seen == [2, 1]
