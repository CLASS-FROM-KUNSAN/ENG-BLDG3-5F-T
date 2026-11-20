"""시간표 모델·config.toml 저장·Swift JSON 호환."""

import json
import sys
from datetime import date, datetime

import pytest

from syncnote.recorder import schedule as sched
from syncnote.timetable import model as M

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _tt():
    return M.Timetable(
        [M.Course("자료구조", ["wed", "mon"], "09:00", "10:50", room="공학관 401호",
                  color="#E74C3C", course_id="C003"),
         M.Course("영어2", ["tue"], "11:00", "12:50", language="en")],
        [M.Holiday(date(2026, 12, 25), "성탄절")],
    )


CONFIG = """# 내 설정 — 이 주석은 살아남아야 합니다
[general]
log_level = "INFO"

[recording]
enabled = true   # 끄지 마세요

[recording.video]
fps = 15
"""


def test_rows_one_per_day_sorted_and_roundtrip():
    tt = _tt()
    rows = tt.to_rows()
    assert [(r["day"], r["name"]) for r in rows] == [
        ("mon", "자료구조"), ("tue", "영어2"), ("wed", "자료구조")]
    assert rows[1]["language"] == "en" and "color" not in rows[1]   # 기본색은 생략
    back = M.Timetable.from_rows(rows, tt.holiday_rows())
    assert [(c.name, c.days) for c in back.courses] == [
        ("자료구조", ["mon", "wed"]), ("영어2", ["tue"])]
    assert back.holidays == tt.holidays


def test_save_keeps_everything_else_and_is_readable_by_recorder(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG, encoding="utf-8")
    assert M.save(p, _tt()) == 0
    text = p.read_text(encoding="utf-8")
    assert text.startswith(CONFIG.rstrip("\n"))          # 앞부분 한 글자도 안 바뀜
    assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == CONFIG
    data = tomllib.loads(text)
    assert data["recording"]["enabled"] is True and data["recording"]["video"]["fps"] == 15
    slots = sched.parse(data["recording"]["timetable"])
    assert {str(s) for s in slots} >= {"월 09:00-10:50  자료구조", "화 11:00-12:50  영어2  [en]"}
    assert sched.holidays(data["recording"]["holidays"]) == {date(2026, 12, 25)}


def test_second_save_replaces_block_not_appends(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG, encoding="utf-8")
    M.save(p, _tt())
    tt = M.load(p)
    tt.courses.pop()
    M.save(p, tt)
    text = p.read_text(encoding="utf-8")
    assert text.count(M.BEGIN) == 1 and text.count(M.END) == 1
    assert [c.name for c in M.load(p).courses] == ["자료구조"]


def test_hand_written_tables_are_commented_not_deleted(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG + '\n[[recording.timetable]]\nname = "옛수업"\nday = "fri"\n'
                 'start = "13:00"\nend = "14:00"\n\n[stream]\nport = 8080\n', encoding="utf-8")
    assert M.save(p, _tt()) == 1
    text = p.read_text(encoding="utf-8")
    assert '# (시간표 편집기로 옮김) name = "옛수업"' in text
    data = tomllib.loads(text)
    assert "옛수업" not in {r["name"] for r in data["recording"]["timetable"]}
    assert data["stream"]["port"] == 8080                  # 뒤 섹션은 그대로


def test_crlf_files_stay_crlf(tmp_path):
    p = tmp_path / "config.toml"
    p.write_bytes(CONFIG.replace("\n", "\r\n").encode("utf-8"))
    M.save(p, _tt())
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_missing_file_is_created(tmp_path):
    p = tmp_path / "new" / "config.toml"
    M.save(p, _tt())
    assert len(M.load(p).courses) == 2


def test_invalid_course_is_refused_and_file_untouched(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG, encoding="utf-8")
    bad = M.Timetable([M.Course("거꾸로", ["mon"], "11:00", "10:00")])
    with pytest.raises(M.TimetableError, match="종료"):
        M.save(p, bad)
    assert p.read_text(encoding="utf-8") == CONFIG


def test_names_with_quotes_survive(tmp_path):
    p = tmp_path / "config.toml"
    tt = M.Timetable([M.Course('C "심화" \\ 실습', ["thu"], "15:00", "16:00")])
    M.save(p, tt)
    assert M.load(p).courses[0].name == 'C "심화" \\ 실습'


def test_unterminated_block_is_an_error_not_a_wipe(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG + M.BEGIN + "\n[[recording.timetable]]\n", encoding="utf-8")
    with pytest.raises(M.TimetableError, match="끝 표시"):
        M.save(p, _tt())


def test_overlaps_are_detected_per_day():
    tt = M.Timetable([M.Course("A", ["mon", "tue"], "09:00", "10:30"),
                      M.Course("B", ["mon"], "10:00", "11:00"),
                      M.Course("C", ["tue"], "10:30", "11:00")])   # 맞닿기만 함
    assert [(d, a.name, b.name) for d, a, b in tt.overlaps()] == [("mon", "A", "B")]


def test_swift_json_roundtrip_and_weekend_warning():
    tt = _tt()
    tt.courses.append(M.Course("주말특강", ["sat"], "10:00", "12:00"))
    text, warnings = M.to_swift_json(tt)
    root = json.loads(text)
    assert root["courses"][0]["weekdays"] == "1,3"
    assert root["courses"][0]["startHour"] == "9" and root["courses"][0]["endMinute"] == "50"
    assert root["courses"][2]["weekdays"] == "" and warnings
    back = M.from_swift_json(text)
    assert [(c.name, c.days, c.start, c.end) for c in back.courses[:2]] == [
        ("자료구조", ["mon", "wed"], "09:00", "10:50"), ("영어2", ["tue"], "11:00", "12:50")]
    assert back.holidays[0].day == date(2026, 12, 25)


def test_swift_json_as_the_app_writes_it():
    ts = int(datetime(2026, 3, 1).timestamp())
    text = json.dumps({"courses": [{"name": "운동과건강", "professor": "김교수",
                                    "room": "체육관", "colorHex": "#2ECC71",
                                    "weekdays": "2,4", "startHour": "13",
                                    "startMinute": "0", "endHour": "14",
                                    "endMinute": "15"}],
                       "holidays": [{"name": "삼일절", "date": str(ts)}]})
    tt = M.from_swift_json(text)
    c = tt.courses[0]
    assert (c.days, c.start, c.end, c.color, c.professor) == (
        ["tue", "thu"], "13:00", "14:15", "#2ECC71", "김교수")
    assert tt.holidays == [M.Holiday(date(2026, 3, 1), "삼일절")]


def test_swift_json_garbage_is_a_clear_error():
    with pytest.raises(M.TimetableError, match="JSON"):
        M.from_swift_json("{not json")


def test_holiday_paste_formats():
    text = ("1/1 신정\n3월 1일(일) 삼일절\n어린이날 5.5\n2027-01-01 내년 신정\n"
            "날짜 없는 줄\n13/40 없는 날짜\n1/1 중복")
    got = M.parse_holiday_text(text, 2026)
    assert [(h.day, h.name) for h in got] == [
        (date(2026, 1, 1), "신정"), (date(2026, 3, 1), "삼일절"),
        (date(2026, 5, 5), "어린이날"), (date(2027, 1, 1), "내년 신정")]


def test_schedule_skips_holidays():
    slots = sched.parse([{"name": "자료구조", "day": "fri", "start": "09:00", "end": "10:00"}])
    now = datetime(2026, 12, 24, 12, 0)          # 목요일 낮
    assert sched.next_occurrence(slots, now=now).start.date() == date(2026, 12, 25)
    nxt = sched.next_occurrence(slots, now=now, skip_days={date(2026, 12, 25)})
    assert nxt.start.date() == date(2027, 1, 1)
    # 공휴일이 여러 주 이어져도 그 뒤 수업을 찾음
    many = {date(2026, 12, 25), date(2027, 1, 1)}
    assert sched.next_occurrence(slots, now=now, skip_days=many).start.date() == date(2027, 1, 8)


# -- 다가오는 일정 · 공휴일 편집 -------------------------------------------
ROWS = [{"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:50"},
        {"name": "영어2", "day": "wed", "start": "11:00", "end": "12:50"}]


def test_upcoming_lists_classes_and_skipped_holidays():
    now = datetime(2026, 11, 20, 12, 0)                         # 금요일
    hol = [{"date": "2026-11-23", "name": "개교기념일"}]          # 다음 월요일
    got = M.upcoming(ROWS, hol, now=now, count=3)
    assert [(u.start.strftime("%m-%d %H:%M"), u.name, u.skipped_for) for u in got] == [
        ("11-23 09:00", "자료구조", "개교기념일"),
        ("11-25 11:00", "영어2", None),
        ("11-30 09:00", "자료구조", None),
        ("12-02 11:00", "영어2", None)]


def test_upcoming_includes_a_class_in_progress_and_trims_trailing_skips():
    now = datetime(2026, 11, 23, 10, 0)                          # 월 자료구조 수업 중
    got = M.upcoming(ROWS, [{"date": "2026-11-25"}], now=now, count=1)
    assert [(u.name, u.skipped) for u in got] == [("자료구조", False)]


def test_upcoming_uses_the_same_parser_as_the_recorder():
    bad = ROWS + [{"name": "깨진", "day": "funday", "start": "09:00", "end": "10:00"}]
    names = {u.name for u in M.upcoming(bad, [], now=datetime(2026, 11, 20), count=4)}
    assert "깨진" not in names


def test_edit_holiday_rejects_a_date_already_taken():
    tt = M.Timetable([], [M.Holiday(date(2026, 12, 25), "성탄절"),
                          M.Holiday(date(2027, 1, 1), "신정")])
    moved = M.edit_holiday(tt, date(2026, 12, 25), M.Holiday(date(2026, 12, 24), " 이브 "))
    assert [(h.day, h.name) for h in moved.holidays][0] == (date(2026, 12, 24), "이브")
    assert tt.holidays[0].day == date(2026, 12, 25)               # 원본은 그대로
    with pytest.raises(M.TimetableError, match="이미"):
        M.edit_holiday(tt, date(2026, 12, 25), M.Holiday(date(2027, 1, 1), "x"))
    assert [h.day for h in M.remove_holidays(tt, [date(2027, 1, 1)]).holidays] == [
        date(2026, 12, 25)]


def test_saved_rows_reads_the_file_not_memory(tmp_path):
    p = tmp_path / "config.toml"
    M.save(p, _tt())
    rows, hrows = M.saved_rows(p)
    assert rows == _tt().to_rows() and hrows == _tt().holiday_rows()
    assert M.saved_rows(tmp_path / "없음.toml") == ([], [])


def test_schedule_parse_skips_broken_rows_instead_of_crashing_later():
    rows = [{"name": "깨진요일", "day": "funday", "start": "09:00", "end": "10:00"},
            {"name": "깨진시각", "day": "mon", "start": "25:99", "end": "10:00"},
            {"name": "정상", "day": "mon", "start": "09:00", "end": "10:00"}]
    slots = sched.parse(rows)
    assert [s.name for s in slots] == ["정상"]
    assert sched.next_occurrence(slots, now=datetime(2026, 11, 20)) is not None


# -- 다른 곳에서 바뀐 파일 -------------------------------------------------
def test_disk_change_kinds(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(CONFIG, encoding="utf-8")
    M.save(p, _tt())
    base = M.DiskState.read(p)
    assert M.disk_change(p, base) == "same"
    p.write_text(p.read_text(encoding="utf-8").replace('log_level = "INFO"', 'log_level = "DEBUG"'),
                 encoding="utf-8")
    assert M.disk_change(p, base) == "other"
    tt = _tt()
    tt.courses[0].end = "11:00"
    M.save(p, tt)
    assert M.disk_change(p, base) == "timetable"
    p.write_text("[[broken", encoding="utf-8")
    assert M.disk_change(p, base) == "broken"


def test_same_timetable_written_by_hand_is_not_a_change(tmp_path):
    """편집기 블록이든 손으로 쓴 표든, 뜻이 같으면 같은 시간표로 봅니다."""
    a = M.canonical(_tt().to_rows(), _tt().holiday_rows())
    rows = list(reversed(_tt().to_rows()))
    assert M.canonical(rows, _tt().holiday_rows()) == a
