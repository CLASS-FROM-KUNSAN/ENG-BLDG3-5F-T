"""SQLite 봉인 저장소 — 평문 at-rest 0, 복호 라운드트립, AAD 무결성.

cryptography 는 CI 에 없을 수 있어 importorskip. 실기(cryptography 설치)에서는
실제 AES-GCM 으로 돌고, 디스크에 평문이 없음을 바이트로 확인합니다.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("cryptography", reason="cryptography 미설치")

from cryptography.exceptions import InvalidTag  # noqa: E402

from gymhall.activity.store import SealedStore  # noqa: E402

DEK = b"\x01" * 32


def ev(value="이진 탐색 트리", name="FullName", doc="d1", ts=10):
    return {"adapter": "pdfjs",
            "scope": {"origin": "https://acrobat.adobe.com", "doc_hash": doc, "field_path": name},
            "field": {"name": name, "type": "text"},
            "value": value, "commit_reason": "change", "ts": ts}


def test_seal_then_decrypt_roundtrip(tmp_path):
    store = SealedStore.open(tmp_path / "a.db", DEK)
    store.seal(ev(value="비밀 메모"))
    store.seal(ev(value="두번째", ts=20))
    out = list(store.decrypt_all())
    assert [e["value"] for e in out] == ["비밀 메모", "두번째"]
    assert store.count() == 2


def test_no_plaintext_on_disk(tmp_path):
    store = SealedStore.open(tmp_path / "a.db", DEK)
    store.seal(ev(value="평문이보이면안됨", name="SecretField"))
    store.close()
    raw = (tmp_path / "a.db").read_bytes()
    assert "평문이보이면안됨".encode() not in raw
    assert b"SecretField" not in raw
    # doc_hash/adapter 는 평문 메타(정렬·질의용)라 보여도 됩니다.
    assert b"pdfjs" in raw


def test_tampered_ciphertext_fails_to_decrypt(tmp_path):
    store = SealedStore.open(tmp_path / "a.db", DEK)
    store.seal(ev())
    row_id = store.conn.execute("SELECT id, ciphertext FROM events").fetchone()
    tampered = bytes([row_id[1][0] ^ 0xFF]) + row_id[1][1:]
    store.conn.execute("UPDATE events SET ciphertext=? WHERE id=?", (tampered, row_id[0]))
    with pytest.raises(InvalidTag):
        list(store.decrypt_all())


def test_wrong_doc_hash_aad_breaks_decrypt(tmp_path):
    store = SealedStore.open(tmp_path / "a.db", DEK)
    store.seal(ev(doc="real"))
    store.conn.execute("UPDATE events SET doc_hash='forged'")
    with pytest.raises(InvalidTag):
        list(store.decrypt_all())


def test_wrong_dek_cannot_read(tmp_path):
    SealedStore.open(tmp_path / "a.db", DEK).seal(ev(value="x"))
    other = SealedStore.open(tmp_path / "a.db", b"\x02" * 32)
    with pytest.raises(InvalidTag):
        list(other.decrypt_all())


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 는 Windows 전용")
def test_kms_dek_roundtrip_via_dpapi(tmp_path):
    from gymhall.activity import kms

    path = tmp_path / "dek.bin"
    dek1 = kms.load_or_create(path)
    assert len(dek1) == kms.DEK_BYTES
    assert path.read_bytes() != dek1          # 봉인본이지 평문 DEK 가 아님
    dek2 = kms.load_or_create(path)           # 두 번째 호출은 같은 DEK
    assert dek1 == dek2
