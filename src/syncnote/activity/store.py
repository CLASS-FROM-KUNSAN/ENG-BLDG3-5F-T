"""SQLite 봉인 저장소 — 평문 at-rest 0.

이벤트는 AES-GCM(DEK)로 암호화해 암호문만 SQLite 에 넣습니다. 평문 JSON 은
메모리에만 잠깐 존재하고 디스크·로그엔 안 남습니다. 녹화·전사와 같은 타임라인에
얹도록 `t_ms`/`doc_hash` 를 평문 메타로 둬, 복호 없이 시간순 질의가 됩니다
(단 필드 값과 필드명은 암호문 안).

게이트/DEK 는 `open_sealed()` 가 한 번 강제합니다 — 게이트 실패 → DEK 없음 →
저장소 없음 → 아무것도 봉인되지 않음. `SealedStore` 자체는 DEK 를 주입받아
게이트 없이도 단위 테스트할 수 있습니다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  t_ms INTEGER NOT NULL,
  adapter TEXT NOT NULL,
  doc_hash TEXT NOT NULL,
  commit_reason TEXT NOT NULL,
  nonce BLOB NOT NULL,
  ciphertext BLOB NOT NULL      -- AES-GCM(평문=JSON(event)), aad=doc_hash
);
CREATE INDEX IF NOT EXISTS idx_events_doc ON events(doc_hash, t_ms);
PRAGMA user_version = 1;
"""


class StoreError(Exception):
    pass


def _aesgcm(dek: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 실기/CI 에 cryptography 가 있으면 안 옴
        raise StoreError("cryptography 패키지가 필요합니다: pip install cryptography") from exc
    return AESGCM(dek)


class SealedStore:
    def __init__(self, conn: sqlite3.Connection, dek: bytes) -> None:
        self.conn = conn
        self._aes = _aesgcm(dek)

    @classmethod
    def open(cls, db_path: str | Path, dek: bytes) -> SealedStore:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.executescript(SCHEMA)
        return cls(conn, dek)

    def seal(self, event: dict[str, Any]) -> None:
        doc_hash = str(event.get("scope", {}).get("doc_hash", ""))
        nonce = os.urandom(12)
        plaintext = json.dumps(event, ensure_ascii=False).encode("utf-8")
        ct = self._aes.encrypt(nonce, plaintext, doc_hash.encode("utf-8"))
        self.conn.execute(
            "INSERT INTO events(t_ms, adapter, doc_hash, commit_reason, nonce, ciphertext)"
            " VALUES (?,?,?,?,?,?)",
            (int(event.get("ts", 0)), str(event.get("adapter", "")), doc_hash,
             str(event.get("commit_reason", "")), nonce, ct),
        )

    def decrypt_all(self) -> Iterator[dict[str, Any]]:
        """복호 — 게이트 확인은 호출부(CLI)의 책임입니다(require_for_read)."""
        cur = self.conn.execute(
            "SELECT doc_hash, nonce, ciphertext FROM events ORDER BY t_ms, id")
        for doc_hash, nonce, ct in cur:
            plaintext = self._aes.decrypt(bytes(nonce), bytes(ct), doc_hash.encode("utf-8"))
            yield json.loads(plaintext.decode("utf-8"))

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def close(self) -> None:
        self.conn.close()


def paths(cfg) -> tuple[str, str]:
    """(db_path, dek_path). 비어 있으면 <데이터>/activity/ 아래 기본값."""
    data_dir = Path(cfg.data_dir) / "activity"
    db_path = str(cfg.get("activity.store.db_path") or (data_dir / "activity.db"))
    dek_path = str(cfg.get("activity.store.dek_path") or (data_dir / "dek.bin"))
    return db_path, dek_path


def open_sealed(cfg) -> SealedStore:
    """게이트 통과(쓰기) → DEK 봉인 로드 → 저장소 열기. 저장 경로의 단 하나의 입구."""
    from . import gate, kms

    gate.require_for_write(cfg.section("activity.gate"))   # 실패 → GateError
    db_path, dek_path = paths(cfg)
    dek = kms.load_or_create(dek_path)
    return SealedStore.open(db_path, dek)
