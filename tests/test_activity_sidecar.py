"""C(데스크톱 Acrobat) 사이드카 워처 — 받는 쪽.

Acrobat 없이 합성 사이드카로 검증합니다(사이드카 계약이 경계). 봉인·게이트 없이
FakeStore 로 정책→봉인 호출 계약과 파일 수명(읽고 즉시 삭제, sweep)을 봅니다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from gymhall.activity import sidecar
from gymhall.activity.policy import FF_PASSWORD, Policy


class FakeStore:
    def __init__(self):
        self.sealed = []

    def seal(self, event):
        self.sealed.append(event)


class FakeState:
    def __init__(self):
        self.seen = set()

    def is_downloaded(self, key):
        return key in self.seen

    def mark_downloaded(self, key):
        self.seen.add(key)

    def save(self):
        pass


def policy():
    return Policy(process_scope=True, name_blacklist=("*ssn*",))


def card(**over):
    base = {"v": 1, "adapter": "acrobat_js", "doc_sha": "sha1", "doc_hash": "d1", "ts": 5,
            "fields": [{"name": "FullName", "type": "text", "flags": 0, "value": "홍길동"}]}
    base.update(over)
    return base


def write(staging: Path, name: str, data: dict):
    staging.mkdir(parents=True, exist_ok=True)
    (staging / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# -- process_sidecar ----------------------------------------------------------
def test_normal_field_is_sealed():
    store, state = FakeStore(), FakeState()
    out = sidecar.process_sidecar(card(), store, policy(), state)
    assert out == ["sealed"] and len(store.sealed) == 1
    assert store.sealed[0]["scope"]["origin"] is None   # 데스크톱=프로세스 스코프


def test_password_field_in_sidecar_is_dropped():
    store = FakeStore()
    data = card(fields=[{"name": "pw", "type": "text", "flags": FF_PASSWORD, "value": "SECRET"}])
    out = sidecar.process_sidecar(data, store, policy(), FakeState())
    assert out == ["drop:password_field"] and store.sealed == []


def test_unsupported_is_reported_not_sealed():
    store = FakeStore()
    for kind in ("encrypted", "xfa", "nofields"):
        out = sidecar.process_sidecar({"v": 1, "adapter": "acrobat_js", "unsupported": kind},
                                      store, policy(), FakeState())
        assert out == [f"unsupported:{kind}"]
    assert store.sealed == []


def test_duplicate_doc_sha_sealed_once():
    store, state = FakeStore(), FakeState()
    assert sidecar.process_sidecar(card(), store, policy(), state) == ["sealed"]
    assert sidecar.process_sidecar(card(), store, policy(), state) == ["dup"]
    assert len(store.sealed) == 1


def test_bad_version_rejected():
    store = FakeStore()
    assert sidecar.process_sidecar({"v": 2}, store, policy(), FakeState()) == ["bad_version"]
    assert store.sealed == []


# -- process_dir: 파일은 어떤 경우에도 삭제 -----------------------------------
def test_process_dir_seals_and_deletes(tmp_path):
    staging = tmp_path / ".staging"
    write(staging, "a.json", card())
    write(staging, "b.json", card(doc_sha="sha2",
          fields=[{"name": "pw", "flags": FF_PASSWORD, "value": "x"}]))
    store = FakeStore()
    tally = sidecar.process_dir(staging, store, policy(), FakeState())
    assert tally["files"] == 2 and tally["sealed"] == 1 and tally["dropped"] == 1
    assert list(staging.glob("*.json")) == []        # 평문 사이드카 전부 삭제


def test_malformed_sidecar_is_still_deleted(tmp_path):
    staging = tmp_path / ".staging"
    staging.mkdir()
    (staging / "bad.json").write_text("{not json", encoding="utf-8")
    tally = sidecar.process_dir(staging, FakeStore(), policy(), FakeState())
    assert tally["errors"] == 1
    assert list(staging.glob("*.json")) == []        # 깨진 것도 평문이라 삭제


# -- sweep: 묵은 사이드카 제거 ------------------------------------------------
def test_sweep_removes_stale_keeps_fresh(tmp_path):
    staging = tmp_path / ".staging"
    write(staging, "fresh.json", card())
    write(staging, "stale.json", card())
    old = time.time() - 3600
    os.utime(staging / "stale.json", (old, old))
    removed = sidecar.sweep(staging, max_age_sec=600)
    assert removed == 1
    names = {p.name for p in staging.glob("*.json")}
    assert names == {"fresh.json"}


def test_sweep_on_missing_dir_is_zero(tmp_path):
    assert sidecar.sweep(tmp_path / "nope", 1) == 0
