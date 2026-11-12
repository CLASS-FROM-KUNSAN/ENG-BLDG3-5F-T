"""시간표 파싱과 다음 수업 계산."""

from __future__ import annotations

from datetime import datetime, timedelta

from gymhall.recorder import schedule as S

TIMETABLE = [
    {"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:50"},
    {"name": "운영체제", "day": "wed", "start": "13:00", "end": "14:50"},
    {"name": "야간실습", "day": "fri", "start": "23:30", "end": "01:20"},
]


def test_parse_valid():
    slots = S.parse(TIMETABLE)
    assert len(slots) == 3
    assert slots[0].weekday == 0
    assert slots[1].weekday == 2


def test_parse_skips_broken_entries_without_dying():
    """시간표 한 줄이 잘못됐다고 전체 녹화를 막으면 안 됩니다."""
    slots = S.parse(TIMETABLE + [{"name": "x", "day": "오요일"}, {"nope": 1}])
    assert len(slots) == 3


def test_midnight_crossing_slot():
    """금 23:30 ~ 01:20 은 토요일 새벽에 끝납니다."""
    slot = S.parse(TIMETABLE)[2]
    start, end = slot.occurrence(datetime(2026, 11, 13).date())  # 금요일
    assert end > start
    assert (end - start) == timedelta(hours=1, minutes=50)
    assert end.day == 14


def test_next_occurrence_picks_the_nearest():
    slots = S.parse(TIMETABLE)
    now = datetime(2026, 11, 11, 8, 0)  # 수요일 아침
    nxt = S.next_occurrence(slots, now=now)
    assert nxt is not None
    assert nxt.slot.name == "운영체제"
    assert nxt.start == datetime(2026, 11, 11, 13, 0)


def test_next_occurrence_skips_finished_class():
    slots = S.parse(TIMETABLE)
    now = datetime(2026, 11, 11, 15, 0)  # 수요일 수업 끝난 뒤
    nxt = S.next_occurrence(slots, now=now)
    assert nxt.slot.name == "야간실습"


def test_ongoing_class_is_returned():
    """수업 중에 프로그램을 켰다면 지금 수업을 잡아야 합니다."""
    slots = S.parse(TIMETABLE)
    now = datetime(2026, 11, 11, 13, 30)
    nxt = S.next_occurrence(slots, now=now)
    assert nxt.slot.name == "운영체제"
    assert nxt.start <= now < nxt.end


def test_pre_and_post_roll_extend_the_window():
    slots = S.parse(TIMETABLE)
    now = datetime(2026, 11, 11, 8, 0)
    plain = S.next_occurrence(slots, now=now)
    padded = S.next_occurrence(slots, now=now, pre_roll_min=5, post_roll_min=10)
    assert padded.start == plain.start - timedelta(minutes=5)
    assert padded.end == plain.end + timedelta(minutes=10)
    assert padded.minutes == plain.minutes + 15


def test_wraps_to_next_week():
    slots = S.parse([TIMETABLE[0]])          # 월요일 수업만
    now = datetime(2026, 11, 11, 12, 0)      # 수요일
    nxt = S.next_occurrence(slots, now=now)
    assert nxt.start.weekday() == 0
    assert nxt.start > now


def test_empty_timetable():
    assert S.next_occurrence([], now=datetime(2026, 11, 11)) is None


def test_describe_empty_tells_user_what_to_do():
    text = S.describe([])
    assert "recording.timetable" in text


def test_describe_lists_slots_sorted():
    text = S.describe(S.parse(TIMETABLE))
    assert "자료구조" in text and "운영체제" in text
    assert text.index("월") < text.index("수")


def test_wait_until_returns_immediately_if_past():
    assert S.wait_until(datetime.now() - timedelta(seconds=1)) is True


def test_wait_until_respects_stop():
    future = datetime.now() + timedelta(hours=1)
    assert S.wait_until(future, stop_check=lambda: True, poll_sec=0.01) is False


# ---------------------------------------------------------------------------
# 과목별 언어
# ---------------------------------------------------------------------------
def test_timetable_language_is_parsed():
    """영어 수업을 ko 로 전사하면 전사본이 통째로 못 쓰게 됩니다."""
    slots = S.parse([
        {"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:50"},
        {"name": "영어2", "day": "tue", "start": "11:00", "end": "12:50", "language": "en"},
        {"name": "웹프로그래밍", "day": "wed", "start": "13:00", "end": "14:50",
         "language": ["ko", "en"]},
    ])
    assert slots[0].language is None       # 전역 설정을 씁니다
    assert slots[1].language == "en"
    assert slots[2].language == "ko+en"    # 목록은 합쳐서 넘깁니다


def test_describe_shows_language():
    slots = S.parse([
        {"name": "영어2", "day": "tue", "start": "11:00", "end": "12:50", "language": "en"},
    ])
    assert "[en]" in S.describe(slots)


def test_language_is_optional():
    slots = S.parse(TIMETABLE)
    assert all(s.language is None for s in slots)
