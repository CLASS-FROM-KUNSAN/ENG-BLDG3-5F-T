"""eClass 과목 목록(코드 ↔ 이름) 저장·합치기·제안."""

import json
from types import SimpleNamespace

from syncnote.eclass import catalog as C


def _c(cid, name, prof="홍교수", open_type="normal"):
    return SimpleNamespace(course_id=cid, name=name, professor=prof, open_type=open_type)


def test_save_and_load_roundtrip_without_professor(tmp_path):
    C.save(tmp_path, "2026-2", [_c("C001", "운동과건강"), _c("C002", "영어2", open_type="lesson"),
                                _c("C001", "중복"), _c("", "코드없음")])
    raw = (tmp_path / C.FILE_NAME).read_text(encoding="utf-8")
    assert "홍교수" not in raw                       # 사람 이름은 남기지 않음
    data = json.loads(raw)
    assert data["terms"]["2026-2"]["courses"] == [
        {"id": "C001", "name": "운동과건강"},
        {"id": "C002", "name": "영어2", "open_type": "lesson"}]
    assert [(e.id, e.name, e.term, e.source) for e in C.load(tmp_path)] == [
        ("C001", "운동과건강", "2026-2", "eclass"), ("C002", "영어2", "2026-2", "eclass")]


def test_other_terms_are_kept_and_latest_comes_first(tmp_path):
    C.save(tmp_path, "2026-1", [_c("A1", "옛과목")])
    C.save(tmp_path, "2026-2", [_c("B1", "새과목")])
    C.save(tmp_path, "2026-2", [_c("B2", "다시 수집")])     # 같은 학기는 교체
    assert [e.id for e in C.load(tmp_path)] == ["B2", "A1"]


def test_broken_or_missing_file_is_empty_not_an_error(tmp_path):
    assert C.load(tmp_path) == []
    (tmp_path / C.FILE_NAME).write_text("{깨짐", encoding="utf-8")
    assert C.load(tmp_path) == []
    C.save(tmp_path, "t", [_c("X", "복구")])               # 깨진 파일 위에도 저장됨
    assert [e.id for e in C.load(tmp_path)] == ["X"]


def test_folders_skip_non_course_dirs(tmp_path):
    for d in ("2026-2/자료구조", "2026-2/_쪽지", "2026-2/_계정", "2026-1/영어1"):
        (tmp_path / d).mkdir(parents=True)
    assert [(e.name, e.term) for e in C.from_folders(tmp_path)] == [
        ("자료구조", "2026-2"), ("영어1", "2026-1")]


def test_merge_prefers_entries_with_codes():
    got = C.merge([C.Entry("C3", "자료구조", "2026-2", "eclass")],
                  [C.Entry("C3", "자료구조(설정)", "", "config"), C.Entry("", "영어2", "", "config")],
                  [C.Entry("", "자료구조", "2026-2", "folder"), C.Entry("", "영어2", "2026-2", "folder")])
    assert [(e.id, e.name, e.source) for e in got] == [
        ("C3", "자료구조", "eclass"), ("", "영어2", "config")]


def test_gather_reads_state_config_and_folders(tmp_path):
    C.save(tmp_path / "state", "2026-2", [_c("C1", "자료구조")])
    (tmp_path / "eclass" / "2026-2" / "캡스톤디자인").mkdir(parents=True)
    got = C.gather(tmp_path, [{"id": "C9", "name": "운영체제", "repo": "os"}])
    assert [(e.id, e.name) for e in got] == [("C1", "자료구조"), ("C9", "운영체제"),
                                             ("", "캡스톤디자인")]


ENTRIES = [C.Entry("C001", "운동과건강"), C.Entry("C002", "영어2"), C.Entry("C005", "영어1"),
           C.Entry("C003", "자료구조및알고리즘")]


def test_suggest_ignores_spacing_and_syllable_order():
    s = C.suggest("건강 운동과", ENTRIES)
    assert s.entry.id == "C001" and s.score == 1.0 and s.confident


def test_suggest_short_name_and_exact_code():
    assert C.suggest("자료구조", ENTRIES).entry.id == "C003"
    assert C.suggest("아무거나", ENTRIES, course_id="C002").entry.id == "C002"


def test_suggest_flags_close_calls_and_gives_up_on_strangers():
    s = C.suggest("영어", ENTRIES)
    assert s.entry is not None and s.ambiguous and not s.confident
    assert C.suggest("양자역학", ENTRIES).entry is None
    assert C.suggest("", ENTRIES).entry is None and C.suggest("x", []).entry is None


def test_crawler_remembers_the_full_list_and_survives_failures(tmp_path):
    from syncnote.eclass.crawler import Course, Crawler

    fake = SimpleNamespace(layout=SimpleNamespace(state=tmp_path), term="2026-2")
    Crawler._remember_catalog(fake, [Course("C1", "자료구조", "교수"), Course("C2", "영어2")])
    assert [e.id for e in C.load(tmp_path)] == ["C1", "C2"]
    broken = SimpleNamespace(layout=SimpleNamespace(state=tmp_path / "f.txt"), term="t")
    (tmp_path / "f.txt").write_text("파일", encoding="utf-8")   # 폴더 자리에 파일 → 실패
    Crawler._remember_catalog(broken, [Course("C1", "x")])      # 예외 없이 넘어감
