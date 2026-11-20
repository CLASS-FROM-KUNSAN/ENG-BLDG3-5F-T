"""수집 결과가 '숫자는 성공, 실제는 0' 일 때 짚어 주는지.

2026-11-12 실제 GitHub Actions 실행:
  수집 결과: 과목 9개 / 페이지 52 / 글 16 / 첨부 0 / 건너뜀 0
  게시판 27곳 전부 '글이 없습니다'
  '과목정보' 9개 과목 전부 '메뉴가 없습니다'
  → 작업은 성공(exit 0)으로 끝났습니다.
"""

from __future__ import annotations

from syncnote.eclass.crawler import Stats


def _real_run() -> Stats:
    s = Stats(courses=9, pages=52, posts=16, files=0, skipped=0)
    s.boards_seen = 27
    s.boards_empty = 27
    s.menus_missing = {"과목정보": 9}
    return s


def test_the_real_degenerate_run_is_flagged():
    notes = " ".join(_real_run().suspicions())
    assert "게시판 27곳이 전부 0건" in notes
    assert "'과목정보'" in notes


def test_a_healthy_run_is_not_flagged():
    s = Stats(courses=9, pages=52, posts=40, files=23)
    s.boards_seen, s.boards_empty = 27, 5
    assert s.suspicions() == []


def test_one_course_with_nothing_is_not_flagged():
    """과목 하나만 돌렸고 정말 비었을 수 있습니다. 거짓 경보를 내지 않습니다."""
    s = Stats(courses=1, pages=4, posts=1, files=0)
    s.boards_seen, s.boards_empty = 3, 3
    s.menus_missing = {"과목정보": 1}
    assert s.suspicions() == []


def test_some_boards_empty_is_normal():
    s = Stats(courses=9, pages=52, posts=16, files=5)
    s.boards_seen, s.boards_empty = 27, 26
    assert s.suspicions() == []


def test_a_menu_missing_in_only_some_courses_is_normal():
    """과목마다 개설된 메뉴가 다릅니다. 전부에서 빠질 때만 의심합니다."""
    s = Stats(courses=9, pages=52, posts=16, files=5)
    s.boards_seen, s.boards_empty = 27, 10
    s.menus_missing = {"팀활동": 4}
    assert s.suspicions() == []


def test_zero_attachments_alone_is_flagged_only_with_posts():
    s = Stats(courses=9, posts=0, files=0)
    s.boards_seen, s.boards_empty = 0, 0
    assert s.suspicions() == []


# --- '첨부 0' 은 첨부가 달릴 수 있는 글로만 따집니다 ---------------------
def test_the_2026_11_13_run_is_not_flagged():
    """글 1 은 화면 글자(.md 하나, 530 B)였습니다. 첨부가 있을 자리가 아닙니다.

    수집 결과: 과목 9개 / 페이지 75 / 글 1 / 첨부 0 / 건너뜀 141
    """
    s = Stats(courses=9, pages=75, posts=1, files=0, skipped=141)
    s.boards_seen, s.boards_empty = 36, 26
    assert s.suspicions() == []


def test_many_entries_and_no_attachment_is_flagged():
    s = Stats(courses=9, posts=12, files=0)
    s.entries = 8
    s.boards_seen, s.boards_empty = 27, 10
    notes = " ".join(s.suspicions())
    assert "게시글·쪽지 8건" in notes and "첨부는 0개" in notes


def test_attachments_already_downloaded_are_not_zero():
    """같은 첨부는 두 번째부터 '이미 받음' 입니다. 0개로 치면 매일 경고가 뜹니다."""
    s = Stats(courses=9, posts=8, files=0)
    s.entries, s.files_known = 8, 5
    s.boards_seen, s.boards_empty = 27, 10
    assert s.suspicions() == []
