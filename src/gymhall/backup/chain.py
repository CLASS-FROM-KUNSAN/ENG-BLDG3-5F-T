"""홈 백업의 앵커 체인.

업로드할 때마다 "어느 디렉터리의 어느 파일을 어느 묶음에 올렸는지"
를 매니페스트 하나로 만들어 같은 릴리스에 올립니다. 매니페스트는
바로 앞 매니페스트의 sha256 을 품고 있어 사슬이 됩니다.

정리(prune)는 **로컬 기록이 아니라 GitHub 에 올라간 사슬**을 믿습니다.
로컬 상태 파일만 보고 지우면, 업로드가 실패했는데 기록만 남은 경우
원본이 어디에도 없게 됩니다. 사슬의 고리를 하나씩 내려받아 해시가
맞는지 확인한 것만 근거로 씁니다.

이 파일은 업로드 쪽과 정리 쪽이 공유하는 **약속**입니다. 형식을
바꾸면 이미 올라간 사슬을 못 읽게 되므로 SCHEMA 를 올리세요.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA = 1
KIND = "gymhall-home-chain"


class ChainError(Exception):
    """사슬이 끊겼거나 변조됐습니다. 이게 나면 아무것도 지우면 안 됩니다."""


@dataclass(frozen=True)
class Link:
    """매니페스트 하나를 가리키는 고리. 어디에 있고 해시가 무엇인지."""

    tag: str
    asset: str
    sha256: str

    @classmethod
    def from_dict(cls, d: Any) -> Link:
        if not isinstance(d, dict):
            raise ChainError(f"고리 형식이 아닙니다: {d!r}")
        try:
            link = cls(str(d["tag"]), str(d["asset"]), str(d["sha256"]))
        except KeyError as exc:
            raise ChainError(f"고리에 {exc} 가 없습니다") from exc
        if len(link.sha256) != 64:
            raise ChainError(f"sha256 길이가 이상합니다: {link.sha256!r}")
        return link


@dataclass(frozen=True)
class Entry:
    """올라간 파일 하나.

    path   : 홈 기준 상대 경로, '/' 구분 (예: 'Documents/과제/a.hwp')
    sha256 : **묶음에 실제로 써 넣은 바이트**의 해시. 읽는 도중 파일이
             바뀌었어도 올라간 내용과 일치합니다.
    bundle : 이 파일이 들어 있는 에셋 이름 (zip)
    member : zip 안의 이름
    """

    path: str
    size: int
    mtime_ns: int
    sha256: str
    bundle: str
    member: str


def canonical_bytes(manifest: dict[str, Any]) -> bytes:
    """업로드하는 바이트 그대로. 해시는 항상 이 바이트로 계산합니다."""
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(*, machine: str, seq: int, prev: Link | None, tag: str,
                   home: str, created_at: str, bundles: Iterable[dict[str, Any]],
                   entries: Iterable[Entry]) -> dict[str, Any]:
    if seq < 0:
        raise ValueError("seq 는 0 이상입니다")
    if (seq == 0) != (prev is None):
        raise ValueError("첫 고리(seq 0)만 prev 가 없어야 합니다")
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "machine": machine,
        "seq": int(seq),
        "prev": asdict(prev) if prev else None,
        "tag": tag,
        "home": home,
        "created_at": created_at,
        "bundles": [dict(b) for b in bundles],
        "entries": [asdict(e) for e in entries],
    }


def parse_manifest(data: bytes, *, expect: Link) -> dict[str, Any]:
    """내려받은 바이트가 기대한 고리가 맞는지 확인하고 읽습니다."""
    got = digest(data)
    if got != expect.sha256:
        raise ChainError(
            f"매니페스트 해시 불일치: {expect.tag}/{expect.asset} "
            f"기대 {expect.sha256[:12]} 실제 {got[:12]}")
    try:
        m = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChainError(f"매니페스트를 읽을 수 없습니다: {exc}") from exc
    if not isinstance(m, dict) or m.get("kind") != KIND:
        raise ChainError("홈 백업 매니페스트가 아닙니다")
    if m.get("schema") != SCHEMA:
        raise ChainError(f"모르는 schema: {m.get('schema')!r}")
    for key in ("machine", "seq", "tag", "bundles", "entries"):
        if key not in m:
            raise ChainError(f"매니페스트에 {key} 가 없습니다")
    if m["tag"] != expect.tag:
        raise ChainError(f"매니페스트의 tag {m['tag']!r} 가 위치 {expect.tag!r} 와 다릅니다")
    return m


def entries_of(manifest: dict[str, Any]) -> list[Entry]:
    try:
        return [Entry(**e) for e in manifest["entries"]]
    except TypeError as exc:
        raise ChainError(f"항목 형식이 이상합니다: {exc}") from exc


def walk_chain(head: Link, fetch: Callable[[Link], bytes], *, machine: str,
               stop_at: Iterable[str] = (),
               visit: Callable[[dict[str, Any]], None] | None = None,
               ) -> list[dict[str, Any]]:
    """head 에서 처음(seq 0)까지 거슬러 올라가며 고리마다 해시를 확인합니다.

    stop_at 에 있는 해시를 만나면 거기서 멈춥니다(이미 확인한 구간).
    그 고리 자체는 결과에 넣지 않습니다. 최신 것이 앞에 옵니다.

    하나라도 이상하면 ChainError — 일부만 맞는 사슬은 쓰지 않습니다.

    visit 을 주면 확인한 고리를 하나씩 넘기고 **들고 있지 않습니다**
    (반환은 빈 리스트). 매니페스트 하나에 항목이 5만 개라, 처음 정리할
    때 쌓인 고리를 전부 메모리에 올리면 안 됩니다. visit 이 한 일은
    호출자가 ChainError 에서 되돌려야 합니다(HomeState.rollback).
    """
    stop = set(stop_at)
    out: list[dict[str, Any]] = []
    link: Link | None = head
    expect_seq: int | None = None
    seen: set[str] = set()
    while link is not None:
        if link.sha256 in stop:
            break
        if link.sha256 in seen:
            raise ChainError("사슬이 고리를 이룹니다")
        seen.add(link.sha256)
        m = parse_manifest(fetch(link), expect=link)
        if m["machine"] != machine:
            raise ChainError(f"다른 PC 의 사슬입니다: {m['machine']!r} != {machine!r}")
        seq = m["seq"]
        if not isinstance(seq, int) or (expect_seq is not None and seq != expect_seq):
            raise ChainError(f"seq 가 이어지지 않습니다: {seq!r} (기대 {expect_seq!r})")
        prev = m.get("prev")
        if visit is None:
            out.append(m)
        else:
            visit(m)
            del m
        if prev is None:
            if seq != 0:
                raise ChainError(f"seq {seq} 인데 앞 고리가 없습니다")
            break
        link = Link.from_dict(prev)
        expect_seq = seq - 1
    return out
