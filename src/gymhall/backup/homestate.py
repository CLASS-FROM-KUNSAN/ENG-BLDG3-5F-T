"""홈 백업의 로컬 상태 (SQLite).

홈 하나에 파일이 66만 개쯤 됩니다. 이걸 Store(JSON 한 덩어리)에 넣으면
저장할 때마다 수백 MB 를 통째로 파싱·직렬화합니다. SQLite 는 경로
인덱스로 한 건씩 찾고, 바뀐 행만 씁니다.

트랜잭션이 안전장치이기도 합니다. 정리(prune)는 사슬 확인이 끝까지
성공해야만 commit 하고, 중간에 ChainError 가 나면 rollback 으로 아무것도
남기지 않습니다. dry-run 은 commit 을 하지 않습니다.

경로는 항상 홈 기준 상대 POSIX 경로('Documents/과제/a.hwp')입니다.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import astuple, dataclass, fields
from pathlib import Path

from .chain import Link

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS head (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    tag     TEXT NOT NULL,
    asset   TEXT NOT NULL,
    sha256  TEXT NOT NULL,
    seq     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256   TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS verified (
    sha256 TEXT PRIMARY KEY
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS pending (
    path          TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    bundle        TEXT NOT NULL,
    member        TEXT NOT NULL,
    tag           TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    bundle_size   INTEGER NOT NULL,
    bundle_parts  INTEGER NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    bundle_part_sha256 TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class FileRow:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class PendingRow:
    """지울 후보 하나. 확인된 매니페스트의 항목 + 그 묶음 정보."""

    path: str
    size: int
    mtime_ns: int
    sha256: str
    bundle: str
    member: str
    tag: str
    seq: int
    created_at: str
    bundle_size: int
    bundle_parts: int
    bundle_sha256: str
    # 분할 묶음의 조각별 sha256, 쉼표로 이음. 사슬(매니페스트)에서 온 값이라
    # 같은 이름으로 덮인 조각을 GitHub digest 로 가려낼 수 있습니다.
    bundle_part_sha256: str = ""


_PENDING_COLS = ", ".join(f.name for f in fields(PendingRow))


class HomeState:
    """with HomeState.open(path) as st: ... ; 바꾼 것은 st.commit() 해야 남습니다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> HomeState:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 백업과 정리는 lock.exclusive 로 겹치지 않게 하지만, 그래도 잠깐
        # 겹치면 5초(기본) 만에 죽지 말고 기다립니다.
        conn = sqlite3.connect(str(path), isolation_level="DEFERRED", timeout=120)
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending)")}
        if "bundle_part_sha256" not in cols:      # 이 칸 이전에 만든 상태 파일
            conn.execute("ALTER TABLE pending ADD COLUMN "
                         "bundle_part_sha256 TEXT NOT NULL DEFAULT ''")
        conn.commit()
        return cls(conn)

    def __enter__(self) -> HomeState:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        # commit 하지 않은 것은 버립니다. 실수로 반쯤 된 상태가 남지 않게.
        self.conn.rollback()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # -- meta ------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        """commit 은 호출자가 합니다(정리의 트랜잭션에 묶이도록)."""
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # -- 설치 ID ---------------------------------------------------------
    def install_id(self) -> str:
        """이 PC(이 상태 DB)만의 무작위 ID. 처음 부를 때 만들고 바로 저장합니다.

        machine_label 은 사람이 적는 값이라, 설정을 복사하거나 같은
        이미지로 복제한 PC 끼리 겹칩니다. 그러면 두 PC 가 같은 이름의
        에셋을 올려 서로의 묶음을 덮어쓰고, 정리가 유일한 사본을
        지웠습니다(검토에서 재현). 사슬과 에셋 이름에 이 ID 를 넣습니다.
        """
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'install_id'").fetchone()
        if row:
            return str(row[0])
        if self.get_head() is not None:
            # 사슬은 있는데 ID 가 없다 = 이 기능 이전 형식. 지금은 배포 전이라
            # 없는 경우지만, 생기면 사슬과 이름이 어긋나므로 멈춥니다.
            raise RuntimeError("홈 백업 상태에 설치 ID 가 없습니다. 상태 파일을 확인하세요.")
        new = secrets.token_hex(4)
        self.conn.execute("INSERT INTO meta (key, value) VALUES ('install_id', ?)", (new,))
        self.conn.commit()
        return new

    # -- head ------------------------------------------------------------
    def get_head(self) -> tuple[Link, int] | None:
        row = self.conn.execute("SELECT tag, asset, sha256, seq FROM head WHERE id = 1").fetchone()
        if row is None:
            return None
        return Link(row[0], row[1], row[2]), int(row[3])

    def set_head(self, link: Link, seq: int) -> None:
        self.conn.execute(
            "INSERT INTO head (id, tag, asset, sha256, seq) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET tag=excluded.tag, asset=excluded.asset, "
            "sha256=excluded.sha256, seq=excluded.seq",
            (link.tag, link.asset, link.sha256, int(seq)),
        )

    # -- files (증분 캐시: 이미 사슬에 올라간 내용) ------------------------
    def get_file(self, path: str) -> FileRow | None:
        row = self.conn.execute(
            "SELECT path, size, mtime_ns, sha256 FROM files WHERE path = ?", (path,)
        ).fetchone()
        return FileRow(*row) if row else None

    def upsert_files(self, rows: Iterable[FileRow]) -> None:
        self.conn.executemany(
            "INSERT INTO files (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
            "mtime_ns=excluded.mtime_ns, sha256=excluded.sha256",
            (astuple(r) for r in rows),
        )

    def file_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    # -- verified (GitHub 에서 확인을 마친 매니페스트 해시) -----------------
    def verified(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT sha256 FROM verified")}

    def add_verified(self, sha256s: Iterable[str]) -> None:
        self.conn.executemany("INSERT OR IGNORE INTO verified (sha256) VALUES (?)",
                              ((s,) for s in sha256s))

    # -- pending (지울 후보) ------------------------------------------------
    def upsert_pending(self, rows: Iterable[PendingRow]) -> None:
        """같은 경로는 **seq 가 더 큰(새) 쪽이 이깁니다.** 넣는 순서와 무관합니다."""
        updates = ", ".join(f"{c}=excluded.{c}" for c in
                            (f.name for f in fields(PendingRow)) if c != "path")
        self.conn.executemany(
            f"INSERT INTO pending ({_PENDING_COLS}) VALUES ({', '.join('?' * len(fields(PendingRow)))}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates} WHERE excluded.seq >= pending.seq",
            (astuple(r) for r in rows),
        )

    def remove_pending(self, paths: Iterable[str]) -> None:
        self.conn.executemany("DELETE FROM pending WHERE path = ?", ((p,) for p in paths))

    def pending_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def iter_pending(self, batch: int = 2000) -> Iterator[PendingRow]:
        """경로 순으로 조금씩 읽습니다. 도중에 remove_pending 해도 안전합니다."""
        last = ""
        while True:
            rows = self.conn.execute(
                f"SELECT {_PENDING_COLS} FROM pending WHERE path > ? ORDER BY path LIMIT ?",
                (last, batch),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield PendingRow(*row)
            last = rows[-1][0]
