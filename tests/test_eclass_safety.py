"""위험 메뉴 안전장치.

docs/RISKS.md 는 사용자에게 두 가지를 약속합니다.

  1. 시험·설문·토론은 **두 겹의 옵트인**(전체 동의 + 개별)이 있어야만 켜진다
  2. 켜더라도 **상세 페이지에는 절대 들어가지 않는다** (목록 제목만 읽는다)

시험 페이지 진입은 LMS 에 응시 시작으로 기록될 수 있고 **되돌릴 수 없습니다.**
그런데 이 약속을 검사하는 테스트가 한 건도 없었습니다. 가드를 지워도
전체 테스트가 통과했습니다 — 문서가 보장하는 것을 코드가 보장하지
않는 상태였습니다.

아래 테스트는 '가드가 있는지' 가 아니라 **'가드를 없애면 실패하는지'**
를 기준으로 썼습니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gymhall.eclass.crawler import Course, Crawler
from gymhall.eclass.urls import RISKY_MENUS, Site
from gymhall.paths import Layout
from gymhall.state import Store

ALL_MENUS = {
    "lecture_list": True, "syllabus": True, "notice": True,
    "qna": True, "materials": True, "assignment": True,
}


class FakeBrowser:
    """크롤러가 쓰는 최소 인터페이스. 어떤 페이지도 실제로 열지 않습니다."""

    def __init__(self):
        self.page = None
        self.timeout_ms = 1000
        self.visited: list[str] = []

    def goto(self, url, wait="domcontentloaded"):
        self.visited.append(url)


def make_crawler(tmp_path, menus=None, risky=None) -> Crawler:
    layout = Layout(tmp_path)
    layout.ensure()
    return Crawler(
        FakeBrowser(), Site(), layout, Store.open(layout.state / "s.json"),
        term="2026-2학기",
        menus=dict(menus if menus is not None else ALL_MENUS),
        risky=dict(risky or {}),
    )


# ---------------------------------------------------------------------------
# 약속 1: 두 겹 옵트인
# ---------------------------------------------------------------------------
def test_risky_menus_off_by_default(tmp_path):
    """아무것도 켜지 않으면 위험 메뉴는 하나도 안 돕니다."""
    enabled = make_crawler(tmp_path).enabled_menus()
    assert not (set(enabled) & RISKY_MENUS), enabled


@pytest.mark.parametrize("menu", sorted(RISKY_MENUS))
def test_individual_flag_alone_is_not_enough(tmp_path, menu):
    """회귀 방지: 개별 플래그만 켜도 돌면 '두 겹' 약속이 깨집니다.

    사용자가 config 를 훑다가 exam = true 하나만 바꾸는 일은 충분히
    일어납니다. 그 한 번으로 시험 페이지에 접근하면 되돌릴 수 없습니다.
    """
    crawler = make_crawler(
        tmp_path,
        menus={**ALL_MENUS, menu: True},
        risky={menu: True},            # i_understand_the_risk 없음
    )
    assert menu not in crawler.enabled_menus()


@pytest.mark.parametrize("menu", sorted(RISKY_MENUS))
def test_umbrella_flag_alone_is_not_enough(tmp_path, menu):
    """전체 동의만 켜고 개별을 안 켜면 돌지 않습니다."""
    crawler = make_crawler(
        tmp_path, risky={"i_understand_the_risk": True},
    )
    assert menu not in crawler.enabled_menus()


@pytest.mark.parametrize("menu", sorted(RISKY_MENUS))
def test_both_flags_enable_exactly_that_menu(tmp_path, menu):
    """둘 다 켜면 그 메뉴만 켜집니다. 나머지 위험 메뉴는 그대로 꺼져 있어야 합니다."""
    crawler = make_crawler(
        tmp_path, risky={"i_understand_the_risk": True, menu: True},
    )
    enabled = set(crawler.enabled_menus())
    assert menu in enabled
    assert (enabled & RISKY_MENUS) == {menu}, enabled


def test_umbrella_does_not_enable_all_risky(tmp_path):
    """전체 동의가 '전부 켜기' 로 동작하면 안 됩니다."""
    crawler = make_crawler(
        tmp_path,
        risky={"i_understand_the_risk": True, "exam": True},
    )
    enabled = set(crawler.enabled_menus())
    assert "survey" not in enabled
    assert "discussion" not in enabled


def test_safe_menus_are_unaffected(tmp_path):
    """안전장치가 평범한 메뉴를 막아 버리면 안 됩니다."""
    enabled = set(make_crawler(tmp_path).enabled_menus())
    assert {"notice", "materials", "assignment"} <= enabled


# ---------------------------------------------------------------------------
# 약속 2: 상세 페이지에 들어가지 않는다
# ---------------------------------------------------------------------------
def _record_routing(crawler, monkeypatch):
    """crawl_board 가 어떤 인자로 불렸는지 기록합니다."""
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        crawler, "crawl_board",
        lambda course, key, *, titles_only=False: calls.append((key, titles_only)),
    )
    monkeypatch.setattr(crawler, "open_course", lambda course: True)
    monkeypatch.setattr(crawler, "open_menu", lambda course, key: True)
    monkeypatch.setattr(crawler, "save_page_text", lambda course, key, **kw: None)
    return calls


@pytest.mark.parametrize("menu", sorted(RISKY_MENUS))
def test_risky_menu_is_routed_titles_only(tmp_path, monkeypatch, menu):
    """회귀 방지: titles_only=True 가 빠지면 상세 페이지를 열게 됩니다."""
    crawler = make_crawler(
        tmp_path, risky={"i_understand_the_risk": True, menu: True},
    )
    calls = _record_routing(crawler, monkeypatch)
    crawler.crawl_course(Course("2026211043701", "테스트과목"))

    risky_calls = [c for c in calls if c[0] == menu]
    assert risky_calls, f"{menu} 가 아예 호출되지 않았습니다: {calls}"
    assert all(titles_only is True for _key, titles_only in risky_calls), risky_calls


def test_normal_boards_are_not_titles_only(tmp_path, monkeypatch):
    """평범한 게시판까지 제목만 읽으면 첨부를 못 받습니다."""
    crawler = make_crawler(tmp_path)
    calls = _record_routing(crawler, monkeypatch)
    crawler.crawl_course(Course("2026211043701", "테스트과목"))

    for key, titles_only in calls:
        if key in {"notice", "qna", "materials", "assignment"}:
            assert titles_only is False, f"{key} 가 제목만 읽기로 돌았습니다"


def test_titles_only_never_opens_a_post(tmp_path, monkeypatch):
    """목록만 읽는 경로가 상세 글을 한 번도 열지 않는지 직접 확인합니다."""
    crawler = make_crawler(
        tmp_path, risky={"i_understand_the_risk": True, "exam": True},
    )
    opened: list[str] = []
    monkeypatch.setattr(
        crawler, "_open_post",
        lambda *a, **kw: opened.append("열림"),
    )
    monkeypatch.setattr(
        crawler, "read_board_rows",
        lambda: [
            {"index": 0, "no": "2", "title": "중간고사", "date": "2026-10-28",
             "has_file": False, "author": "홍길동", "cells": []},
            {"index": 1, "no": "1", "title": "퀴즈 1", "date": "2026-10-14",
             "has_file": False, "author": "홍길동", "cells": []},
        ],
    )
    monkeypatch.setattr(crawler, "board_page_count", lambda: 1)
    crawler.br.page = type("P", (), {"url": "https://x/list"})()

    crawler.crawl_board(Course("2026211043701", "테스트과목"), "exam", titles_only=True)

    assert opened == [], "목록만 읽어야 하는데 상세 글을 열었습니다"


def test_titles_only_writes_a_list_file(tmp_path, monkeypatch):
    """제목 목록은 파일로 남아야 합니다(수집 자체는 되어야 함)."""
    crawler = make_crawler(
        tmp_path, risky={"i_understand_the_risk": True, "exam": True},
    )
    monkeypatch.setattr(
        crawler, "read_board_rows",
        lambda: [{"index": 0, "no": "1", "title": "중간고사 안내",
                  "date": "2026-10-28", "has_file": False, "author": "홍", "cells": []}],
    )
    monkeypatch.setattr(crawler, "board_page_count", lambda: 1)
    crawler.br.page = type("P", (), {"url": "https://x/list"})()

    crawler.crawl_board(Course("2026211043701", "테스트과목"), "exam", titles_only=True)

    written = list(Path(tmp_path).rglob("*.md"))
    assert written, "목록 파일이 생기지 않았습니다"
    text = written[0].read_text(encoding="utf-8")
    assert "중간고사 안내" in text
    assert "상세 페이지 진입은" in text, "왜 목록만 받았는지 설명이 없습니다"


# ---------------------------------------------------------------------------
# 문서와 코드가 같은 것을 말하는지
# ---------------------------------------------------------------------------
def test_risks_doc_matches_the_code():
    """docs/RISKS.md 가 약속한 동작이 실제 코드 구조와 일치하는지.

    문서가 보장한다고 적은 것을 코드가 보장하지 않으면, 사용자는
    안전하다고 믿고 켭니다.
    """
    repo = Path(__file__).resolve().parents[1]
    risks = (repo / "docs" / "RISKS.md").read_text(encoding="utf-8")
    assert "i_understand_the_risk" in risks
    assert "상세 페이지에는 절대 들어가지 않습니다" in risks
    # 문서가 말하는 세 메뉴가 코드의 집합과 같은지
    for menu in ("시험", "설문", "토론"):
        assert menu in risks
    assert {"exam", "survey", "discussion"} == RISKY_MENUS
