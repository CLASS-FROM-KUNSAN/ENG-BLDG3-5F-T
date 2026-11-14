"""홈 백업 상태(SQLite): commit 한 것만 남고, 새 seq 가 이깁니다."""

from __future__ import annotations

from gymhall.backup.chain import Link
from gymhall.backup.homestate import FileRow, HomeState, PendingRow


def _p(path, seq, sha="a" * 64):
    return PendingRow(path=path, size=1, mtime_ns=1, sha256=sha, bundle="b.zip",
                      member=path, tag=f"home-{seq}", seq=seq, created_at="x",
                      bundle_size=10, bundle_parts=0, bundle_sha256="c" * 64)


def test_uncommitted_changes_are_dropped(tmp_path):
    db = tmp_path / "s.sqlite"
    with HomeState.open(db) as st:
        st.set_head(Link("t", "a", "d" * 64), 0)
        st.upsert_files([FileRow("x", 1, 2, "e" * 64)])
    with HomeState.open(db) as st:
        assert st.get_head() is None
        assert st.get_file("x") is None


def test_committed_changes_persist(tmp_path):
    db = tmp_path / "s.sqlite"
    with HomeState.open(db) as st:
        st.set_head(Link("t", "a", "d" * 64), 3)
        st.upsert_files([FileRow("x", 1, 2, "e" * 64)])
        st.add_verified(["f" * 64, "f" * 64])
        st.commit()
    with HomeState.open(db) as st:
        assert st.get_head() == (Link("t", "a", "d" * 64), 3)
        assert st.get_file("x") == FileRow("x", 1, 2, "e" * 64)
        assert st.verified() == {"f" * 64}


def test_newer_seq_wins_regardless_of_order(tmp_path):
    with HomeState.open(tmp_path / "s.sqlite") as st:
        st.upsert_pending([_p("d/a", 5, "5" * 64)])
        st.upsert_pending([_p("d/a", 2, "2" * 64)])   # 오래된 것이 뒤에 와도
        assert next(st.iter_pending()).sha256 == "5" * 64
        st.upsert_pending([_p("d/a", 7, "7" * 64)])
        assert next(st.iter_pending()).sha256 == "7" * 64


def test_iter_pending_survives_removal_during_iteration(tmp_path):
    with HomeState.open(tmp_path / "s.sqlite") as st:
        st.upsert_pending([_p(f"d/{i:04d}", 0) for i in range(25)])
        seen = []
        for row in st.iter_pending(batch=4):
            seen.append(row.path)
            st.remove_pending([row.path])
        assert len(seen) == 25 and len(set(seen)) == 25
        assert st.pending_count() == 0


def test_scale_lookup_is_indexed(tmp_path):
    """66만 건 규모를 흉내 냅니다(여기선 5만). 전체를 읽지 않고 한 건씩 찾습니다."""
    with HomeState.open(tmp_path / "s.sqlite") as st:
        st.upsert_files(FileRow(f"Downloads/{i:06d}.bin", i, i, "0" * 64)
                        for i in range(50_000))
        st.commit()
        assert st.file_count() == 50_000
        assert st.get_file("Downloads/049999.bin").size == 49_999
        plan = st.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM files WHERE path = ?", ("x",)).fetchall()
        assert any("PRIMARY KEY" in str(r) or "INDEX" in str(r) for r in plan), plan
