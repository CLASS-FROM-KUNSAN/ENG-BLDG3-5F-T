"""겹치는 수업 → 실제 녹화 창 계획(recording.overlap)."""

from datetime import date, datetime

from syncnote.recorder import schedule as S

MON = date(2026, 11, 23)          # 월요일


def _slots(*rows):
    return S.parse([{"name": n, "day": "mon", "start": a, "end": b} for n, a, b in rows])


def _win(ws):
    return [(w.label, f"{w.start:%H:%M}", f"{w.end:%H:%M}") for w in ws]


def _lost(ls):
    return [(x.slot.name, f"{x.start:%H:%M}", f"{x.end:%H:%M}", x.because) for x in ls]


def test_no_overlap_keeps_rolls():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:00"), ("B", "11:00", "12:00")), MON, 2, 3)
    assert _win(ws) == [("A", "08:58", "10:03"), ("B", "10:58", "12:03")] and lost == []


def test_rolls_only_overlap_is_split_between_classes_without_losing_class_time():
    for policy in ("first", "last", "merge"):
        ws, lost = S.plan_day(_slots(("A", "09:00", "10:50"), ("B", "10:50", "12:00")),
                              MON, 2, 3, policy)
        assert _win(ws) == [("A", "08:58", "10:50"), ("B", "10:50", "12:03")], policy
        assert lost == []
    ws, _ = S.plan_day(_slots(("A", "09:00", "10:50"), ("B", "10:52", "12:00")), MON, 2, 3)
    assert _win(ws) == [("A", "08:58", "10:51"), ("B", "10:51", "12:03")]


def test_first_keeps_earlier_class_and_later_starts_after():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00")), MON, 2, 3,
                          "first")
    assert _win(ws) == [("A", "08:58", "10:30"), ("B", "10:30", "11:03")]
    assert _lost(lost) == [("B", "10:00", "10:30", "A")] and lost[0].minutes == 30


def test_first_drops_a_class_fully_inside_another():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "09:30", "10:00")), MON, 2, 3,
                          "first")
    assert _win(ws) == [("A", "08:58", "10:33")]
    assert _lost(lost) == [("B", "09:30", "10:00", "A")]


def test_last_hands_off_at_the_later_class_start():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00")),
                          MON, 2, 3, "last")
    assert _win(ws) == [("A", "08:58", "10:00"), ("B", "10:00", "11:03")]
    assert _lost(lost) == [("A", "10:00", "10:30", "B")]


def test_last_resumes_the_outer_class_after_a_nested_one():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "09:30", "10:00")),
                          MON, 2, 3, "last")
    assert _win(ws) == [("A", "08:58", "09:30"), ("B", "09:30", "10:00"), ("A", "10:00", "10:33")]
    assert ws[2].resumed and _lost(lost) == [("A", "09:30", "10:00", "B")]


def test_merge_records_one_file_with_both_names():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00")),
                          MON, 2, 3, "merge")
    assert _win(ws) == [("A + B", "08:58", "11:03")] and lost == []


def test_chain_of_three_under_first():
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00"),
                                 ("C", "10:45", "12:00")), MON, 2, 3, "first")
    assert _win(ws) == [("A", "08:58", "10:30"), ("B", "10:30", "11:00"), ("C", "11:00", "12:03")]
    assert _lost(lost) == [("B", "10:00", "10:30", "A"), ("C", "10:45", "11:00", "B")]


def test_same_start_is_decided_by_name_and_reported():
    ws, lost = S.plan_day(_slots(("나", "09:00", "10:00"), ("가", "09:00", "10:00")), MON, 0, 0,
                          "first")
    assert _win(ws) == [("가", "09:00", "10:00")] and _lost(lost) == [("나", "09:00", "10:00", "가")]


def test_next_window_follows_the_plan_and_video_start():
    sl = _slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00"))
    a = S.next_window(sl, now=datetime(2026, 11, 23, 8, 0), pre_roll_min=2, post_roll_min=3,
                      policy="first")
    assert (a.name, f"{a.start:%H:%M}", f"{a.end:%H:%M}", f"{a.video_from:%H:%M}") == (
        "A", "08:58", "10:30", "09:00")                  # 화면은 진짜 수업 시작에
    b = S.next_window(sl, now=datetime(2026, 11, 23, 10, 30), pre_roll_min=2, post_roll_min=3,
                      policy="first")
    assert (b.name, f"{b.start:%H:%M}", f"{b.video_from:%H:%M}") == ("B", "10:30", "10:30")
    m = S.next_window(sl, now=datetime(2026, 11, 23, 8, 0), pre_roll_min=2, post_roll_min=3,
                      policy="merge")
    assert m.name == "A + B" and [s.name for s in m.slots] == ["A", "B"]


def test_next_window_finds_a_class_crossing_midnight():
    sl = S.parse([{"name": "야간", "day": "mon", "start": "23:30", "end": "00:30"}])
    w = S.next_window(sl, now=datetime(2026, 11, 24, 0, 10))
    assert w is not None and w.name == "야간" and f"{w.end:%m-%d %H:%M}" == "11-24 00:30"


def test_overlap_report_and_text():
    sl = _slots(("자료구조", "09:00", "10:30"), ("영어2", "10:00", "11:00"))
    rep = S.overlap_report(sl, 2, 3, "first", today=date(2026, 11, 20))
    assert len(rep) == 1 and rep[0][0] == 0
    text = S.describe_lost(*rep[0], "first")
    assert text == "월 영어2 10:00~10:30 (30분) 녹화 안 됨 — 자료구조 녹화 중 · 정책: 앞 수업 우선"
    assert "겹치는 수업" in S.describe(sl, pre_roll_min=2, post_roll_min=3, policy="first")
    assert "겹치는 수업" not in S.describe(sl, pre_roll_min=2, post_roll_min=3)   # 기본 merge
    assert S.overlap_report(sl, 2, 3, "merge", today=date(2026, 11, 20)) == []


def test_record_schedule_uses_the_overlap_plan(monkeypatch, tmp_path):
    """녹화 루프가 실제 계획(next_window)을 따라 이름·화면 시작을 넘기는지."""
    from datetime import timedelta

    from syncnote import cli
    from syncnote.paths import Layout

    class Cfg:                                   # 점 표기 키를 그대로 받는 최소 설정
        machine_label = "테스트PC"

        def __init__(self, d):
            self._d = d

        def get(self, key, default=None):
            return self._d.get(key, default)

    # 시계를 월요일 아침에 고정합니다. 실제 시계를 쓰면 자정 90분 전부터는 B 수업이
    # 다음 날로 넘어가 CI(UTC 23시대)에서만 실패했습니다(수업은 자정을 넘지 않음).
    frozen = datetime(2026, 11, 23, 8, 50)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", _Clock)
    monkeypatch.setattr(S, "datetime", _Clock)
    base = frozen + timedelta(minutes=10)
    day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[base.weekday()]
    hm = lambda d: d.strftime("%H:%M")  # noqa: E731
    rows = [{"name": "A", "day": day, "start": hm(base), "end": hm(base + timedelta(minutes=60))},
            {"name": "B", "day": day, "start": hm(base + timedelta(minutes=30)),
             "end": hm(base + timedelta(minutes=90))}]
    monkeypatch.setattr(cli, "_load", lambda _a: (
        Cfg({"recording.pre_roll_minutes": 2, "recording.post_roll_minutes": 3,
             "recording.overlap": "merge", "recording.timetable": rows,
             "recording.holidays": []}), Layout(tmp_path)))
    monkeypatch.setattr(S, "wait_until", lambda *a, **kw: True)
    monkeypatch.setattr(cli, "_resolve_course", lambda *a, **kw: None)
    seen = {}
    monkeypatch.setattr(cli, "_run_recording",
                        lambda cfg, layout, label, **kw: seen.update(label=label, **kw) or object())

    class _Args:
        show = False
        once = True
        no_upload = True
        dry_run = True

    assert cli.cmd_record_schedule(_Args()) == 0
    assert seen["label"] == "A + B"
    assert 100 < seen["minutes"] < 106
    assert abs(seen["video_not_before"] - base.timestamp()) < 1          # 진짜 수업 시작
    # 기다림을 건너뛰었으니 길이는 지금 ~ 창 끝(B 끝 + post 3분) = 앞둔 10분 + 93분 안팎


def test_default_policy_is_one_take_merge():
    """기본값: 겹치면 앞 수업 시작부터 뒤 수업 끝까지 한 번에(원테이크), 잃는 시간 없음."""
    from syncnote import config as C

    assert S.DEFAULT_POLICY == "merge" and C.DEFAULTS["recording"]["overlap"] == "merge"
    ws, lost = S.plan_day(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00")), MON, 2, 3)
    assert _win(ws) == [("A + B", "08:58", "11:03")] and lost == []
    w = S.next_window(_slots(("A", "09:00", "10:30"), ("B", "10:00", "11:00")),
                      now=datetime(2026, 11, 23, 8, 0), pre_roll_min=2, post_roll_min=3)
    assert [(s.name, f"{a:%H:%M}", f"{b:%H:%M}") for s, a, b in w.classes] == [
        ("A", "09:00", "10:30"), ("B", "10:00", "11:00")]          # 수업별 예정 시각


def test_slot_reads_editor_columns():
    sl = S.parse([{"name": "자료구조", "day": "mon", "start": "09:00", "end": "10:00",
                   "course_id": "C1", "room": "공학관 401호", "professor": "김교수"}])[0]
    assert (sl.course_id, sl.room, sl.professor) == ("C1", "공학관 401호", "김교수")
    assert str(sl) == "월 09:00-10:00  자료구조"                     # 표시는 그대로
