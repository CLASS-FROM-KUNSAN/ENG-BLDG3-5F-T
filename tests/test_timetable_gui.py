"""PySide6 편집기: 화면 없이(offscreen) 실제 마우스 이벤트로 조작."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from syncnote.timetable import gui  # noqa: E402
from syncnote.timetable import model as M  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    M.save(p, M.Timetable([M.Course("자료구조", ["mon", "wed"], "09:00", "10:00")]))
    # 모달 창이 테스트를 멈추지 않게.
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Discard)
    # 예상 못 한 모달 창은 멈추지 말고 바로 실패. (창 맞춤이 드래그 도중 창을
    # 옮겨 클릭이 빈 칸에 떨어지면 '수업 추가' 창이 떠서 테스트가 영원히
    # 멈췄던 적이 있습니다.) 창이 필요한 테스트는 각자 다시 바꿉니다.
    monkeypatch.setattr(gui.CourseEditor, "exec",
                        lambda self: pytest.fail("예상 못 한 수업 창"))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("예상 못 한 메시지 창"))
    for kind in ("critical", "warning", "information"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(
            lambda _p, title, text, *a, _k=kind, **kw: pytest.fail(f"예상 못 한 {_k}: {title}: {text}")))
    w = gui.TimetableWindow(p)
    w.resize(1000, 1400)
    w.show()
    app.processEvents()
    # 창 맞춤(ScreenKeeper)을 먼저 끝내 둡니다. 늦게 끼어들면 드래그 좌표가 어긋남.
    w.keeper.check()
    w.keeper.timer.stop()
    app.processEvents()
    w.keeper.timer.stop()
    yield w
    w.undo.setClean()
    w.close()


def _send(view, kind, scene_pt, buttons, mods=Qt.KeyboardModifier.NoModifier):
    vp = view.viewport()
    local = QPointF(view.mapFromScene(scene_pt))
    btn = Qt.MouseButton.LeftButton if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton
    ev = QMouseEvent(kind, local, QPointF(vp.mapToGlobal(local.toPoint())), btn, buttons, mods)
    QApplication.sendEvent(vp, ev)


def drag(app, view, a: QPointF, b: QPointF, mods=Qt.KeyboardModifier.NoModifier):
    left = Qt.MouseButton.LeftButton
    _send(view, QEvent.Type.MouseButtonPress, a, left, mods)
    mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
    _send(view, QEvent.Type.MouseMove, mid, left, mods)
    _send(view, QEvent.Type.MouseMove, b, left, mods)
    _send(view, QEvent.Type.MouseButtonRelease, b, Qt.MouseButton.NoButton, mods)
    app.processEvents()   # 큐 연결로 넘어온 변경 반영


def _cell(scene, day, minute):
    return QPointF(scene.x_of(day) + gui.DAY_W / 2, scene.y_of(minute))


def test_ui_file_loads_and_editor_roundtrips(app):
    c = M.Course("영어2", ["tue", "thu"], "11:00", "12:50", room="인문관", language="en",
                 color="#9B59B6", course_id="E2")
    ed = gui.CourseEditor(None, c, "t")
    assert ed.course() == c
    ed.name.setText("")
    ed._try_accept()
    assert "수업명" in ed.error.text()         # 빈 이름은 확인 버튼으로도 못 닫음


def test_window_loads_blocks_per_day(win):
    blocks = [i for i in win.scene.items() if isinstance(i, gui.CourseBlock)]
    assert sorted(b.day for b in blocks) == ["mon", "wed"]
    assert win.windowTitle().endswith("config.toml")


def test_drag_empty_cell_creates_course(app, win, monkeypatch):
    def fake_exec(self):
        self.name.setText("운영체제")
        return self.course()
    monkeypatch.setattr(gui.CourseEditor, "exec", fake_exec)
    s = win.scene
    drag(app, win.view, _cell(s, "tue", 13 * 60), _cell(s, "tue", 14 * 60 + 30))
    new = win.tt.courses[-1]
    assert (new.name, new.days, new.start, new.end) == ("운영체제", ["tue"], "13:00", "14:30")
    assert not win.undo.isClean() and win.windowTitle().endswith("*")


def test_drag_block_moves_day_and_time_for_that_occurrence(app, win):
    s = win.scene
    drag(app, win.view, _cell(s, "mon", 9 * 60 + 20), _cell(s, "fri", 10 * 60 + 20))
    c = win.tt.courses[0]
    assert (c.days, c.start, c.end) == (["wed", "fri"], "10:00", "11:00")
    win.undo.undo()
    assert win.tt.courses[0].days == ["mon", "wed"]


def test_shift_drag_moves_only_that_day(app, win):
    s = win.scene
    drag(app, win.view, _cell(s, "wed", 9 * 60 + 20), _cell(s, "wed", 13 * 60 + 20),
         mods=Qt.KeyboardModifier.ShiftModifier)
    got = sorted((c.days, c.start) for c in win.tt.courses)
    assert got == [(["mon"], "09:00"), (["wed"], "13:00")]


def test_drag_bottom_edge_resizes(app, win):
    s = win.scene
    a = QPointF(s.x_of("mon") + gui.DAY_W / 2, s.y_of(10 * 60) - 3)   # 아래 가장자리
    drag(app, win.view, a, QPointF(a.x(), a.y() + 47))                 # 47분 -> 45분 단위
    assert win.tt.courses[0].end == "10:45"
    drag(app, win.view, QPointF(a.x(), s.y_of(10 * 60 + 45) - 3),
         QPointF(a.x(), s.y_of(9 * 60)))                                # 너무 줄이면 최소 10분
    assert win.tt.courses[0].end == "09:10"


def test_save_writes_config_and_marks_clean(app, win):
    win._on_move(0, "mon", "thu", 15 * 60, 16 * 60, False)
    assert win.save()
    assert win.undo.isClean()
    assert M.load(win.path).courses[0].days == ["thu", "wed"] or \
        M.load(win.path).courses[0].days == ["wed", "thu"]


def test_overlap_shows_warning_in_status(app, win):
    new = win.tt.copy()
    new.courses.append(M.Course("겹침", ["mon"], "09:30", "10:30"))
    win._apply(new, "t")
    assert "겹침" in win.statusBar().currentMessage()


def test_blocks_show_their_labels(win):
    """블록 글자 아이템이 GC 로 사라지면 색 상자만 남습니다(실제로 있었던 버그)."""
    b = next(i for i in win.scene.items() if isinstance(i, gui.CourseBlock))
    assert "자료구조" in b.label.toPlainText()
    assert b.label in b.childItems()


def test_overlapping_courses_are_drawn_side_by_side(app, win):
    new = win.tt.copy()
    new.courses.append(M.Course("겹침", ["mon"], "09:30", "10:30"))
    new.courses.append(M.Course("나중", ["mon"], "11:00", "12:00"))
    win._apply(new, "t")
    mon = {b.course.name: b.rect() for b in win.scene.items()
           if isinstance(b, gui.CourseBlock) and b.day == "mon"}
    assert mon["자료구조"].width() < gui.DAY_W / 2 + 1
    assert mon["겹침"].left() > mon["자료구조"].left()
    assert mon["나중"].width() > gui.DAY_W - 10          # 안 겹치는 수업은 칸 전체


def test_dropping_a_block_in_place_changes_nothing(app, win):
    s = win.scene
    p = _cell(s, "mon", 9 * 60 + 20)
    drag(app, win.view, p, QPointF(p.x() + 3, p.y() + 1))   # 5분 단위 안에서 흔들기
    assert win.undo.count() == 0 and win.undo.isClean()
    assert len(win.tt.courses) == 1


def test_dark_palette_keeps_day_names_readable(app, win):
    """다크 모드에서 요일 이름이 흰 배경에 흰 글자로 사라졌던 버그."""
    from PySide6.QtGui import QColor, QPalette
    from PySide6.QtWidgets import QGraphicsTextItem

    old = app.palette()
    dark = QPalette(old)
    for role, c in ((QPalette.ColorRole.Base, "#1E1E1E"), (QPalette.ColorRole.Window, "#202020"),
                    (QPalette.ColorRole.Text, "#F0F0F0"), (QPalette.ColorRole.WindowText, "#F0F0F0")):
        dark.setColor(role, QColor(c))
    try:
        app.setPalette(dark)
        app.processEvents()
        s = win.scene
        assert s.theme.dark and s.backgroundBrush().color().lightness() < 80
        heads = [i for i in s.items() if isinstance(i, QGraphicsTextItem)
                 and i.toPlainText() in ("월", "화", "수", "목", "금")]
        assert len(heads) == 5
        bg = s.backgroundBrush().color().lightness()
        for h in heads:
            assert abs(h.defaultTextColor().lightness() - bg) > 100, h.toPlainText()
    finally:
        app.setPalette(old)
        app.processEvents()
    assert not win.scene.theme.dark


# -- 저장 결과 창 ----------------------------------------------------------
def _upcoming_texts(summary):
    return [summary.next_list.item(i).text() for i in range(summary.next_list.count())]


def test_save_opens_summary_with_next_recordings_from_the_file(app, win):
    from datetime import date as _d
    from datetime import datetime as _dt

    new = win.tt.copy()
    new.holidays.append(M.Holiday(_d(2026, 11, 23), "개교기념일"))
    win._apply(new, "t")
    assert win.save()
    s = win.summary
    assert s is not None and s.isVisible() and not s.isModal()
    assert "저장됨" in s.state.text() and not s.save_btn.isEnabled()
    s.refresh(now=_dt(2026, 11, 20, 12, 0))
    texts = _upcoming_texts(s)
    assert texts[0].startswith("11/23(월) 09:00~10:00  자료구조") and "쉼(개교기념일)" in texts[0]
    assert texts[1].startswith("11/25(수) 09:00~10:00  자료구조") and "쉼" not in texts[1]
    assert s.htable.rowCount() >= 0


def test_summary_add_edit_delete_holidays(app, win, monkeypatch):
    from datetime import date as _d
    from datetime import datetime as _dt

    future = _d.today().replace(year=_d.today().year + 1)
    win.save()
    s = win.summary
    # 추가
    monkeypatch.setattr(gui, "ask_holiday", lambda *_a, **_k: M.Holiday(future, "개교기념일"))
    s.add_btn.click()
    assert s.htable.rowCount() == 1 and s.save_btn.isEnabled()
    assert "저장하지 않은" in s.state.text()
    # 수정 (선택 후 버튼)
    s.htable.selectRow(0)
    monkeypatch.setattr(gui, "ask_holiday", lambda _p, cur: M.Holiday(cur.day, "학교 쉬는 날"))
    s.edit_btn.click()
    assert s.htable.item(0, 1).text() == "학교 쉬는 날"
    assert win.htable.item(0, 1).text() == "학교 쉬는 날"           # 탭도 같이 바뀜
    # 실행 취소가 결과 창에도 반영
    win.undo.undo()
    assert s.htable.item(0, 1).text() == "개교기념일"
    # 삭제
    s.htable.selectRow(0)
    s.del_btn.click()
    assert s.htable.rowCount() == 0 and win.tt.holidays == []
    # 저장 버튼
    s.save_btn.click()
    assert win.undo.isClean() and not s.save_btn.isEnabled()
    s.refresh(now=_dt.now())


def test_holiday_tab_edit_by_double_click(app, win, monkeypatch):
    from datetime import date as _d

    new = win.tt.copy()
    new.holidays.append(M.Holiday(_d(2026, 12, 25), "성탄절"))
    win._apply(new, "t")
    monkeypatch.setattr(gui, "ask_holiday", lambda _p, cur: M.Holiday(_d(2026, 12, 24), cur.name))
    win.htable.cellDoubleClicked.emit(0, 0)
    assert [h.day for h in win.tt.holidays] == [_d(2026, 12, 24)]


def test_closing_with_save_does_not_pop_the_summary(app, win, monkeypatch):
    new = win.tt.copy()
    new.courses[0].name = "바뀜"
    win._apply(new, "t")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Save)
    assert win._confirm_discard()
    assert win.summary is None and win.undo.isClean()


def test_holiday_tab_has_summary_button_top_right(app, win):
    win.tabs.setCurrentIndex(2)
    app.processEvents()
    btn = win.summary_btn
    assert btn.text() == "다가오는 일정 · 공휴일 보기" and btn.isVisible()
    tab = win.tabs.currentWidget()
    pos = btn.mapTo(tab, btn.rect().topRight())
    assert pos.x() > tab.width() * 0.6 and pos.y() < win.htable.mapTo(tab, win.htable.rect().topLeft()).y()
    btn.click()
    assert win.summary is not None and win.summary.isVisible()


# -- 다른 곳에서 바뀐 파일 -------------------------------------------------
def _edit_file(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _choose(monkeypatch, pick):
    """저장 충돌 창에서 pick(텍스트 또는 'cancel') 을 고른 것으로."""
    seen = {}

    def fake_exec(self):
        seen["text"] = self.text()
    def fake_clicked(self):
        if pick == "cancel":
            return self.button(QMessageBox.StandardButton.Cancel)
        return next(b for b in self.buttons() if b.text() == pick)
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", fake_clicked)
    return seen


def _dirty(win):
    new = win.tt.copy()
    new.courses[0].name = "내가 바꾼 이름"
    win._apply(new, "t")


def test_other_settings_changed_elsewhere_save_keeps_them_without_asking(app, win, monkeypatch):
    _dirty(win)
    win.path.write_text(win.path.read_text(encoding="utf-8").replace(
        M.BEGIN, '[stream]\nport = 9999\n\n' + M.BEGIN), encoding="utf-8")
    assert win.check_disk() == "other" and win.banner.isHidden()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: pytest.fail("묻지 않아야 함"))
    assert win.save(show_summary=False)
    text = win.path.read_text(encoding="utf-8")
    assert "port = 9999" in text and "내가 바꾼 이름" in text


def test_timetable_changed_elsewhere_while_editing_shows_banner_and_asks(app, win, monkeypatch):
    _dirty(win)
    _edit_file(win.path, 'end   = "10:00"', 'end   = "10:30"')     # 다른 곳에서 시간 변경
    assert win.check_disk() == "timetable"
    assert win.banner.isVisible() and "덮어씁니다" in win.banner_label.text()

    seen = _choose(monkeypatch, "cancel")
    assert not win.save(show_summary=False)
    assert "바뀌었습니다" in seen["text"]
    assert 'end   = "10:30"' in win.path.read_text(encoding="utf-8")   # 안 덮어씀
    assert not win.undo.isClean()

    _choose(monkeypatch, "내 것으로 덮어쓰기")
    assert win.save(show_summary=False)
    assert "내가 바꾼 이름" in win.path.read_text(encoding="utf-8")
    assert win.banner.isHidden() and win.check_disk() == "same"


def test_reload_choice_takes_the_disk_version(app, win, monkeypatch):
    _dirty(win)
    _edit_file(win.path, 'end   = "10:00"', 'end   = "10:30"')
    _choose(monkeypatch, "디스크 내용 다시 읽기")
    assert not win.save(show_summary=False)
    assert win.tt.courses[0].name == "자료구조" and win.tt.courses[0].end == "10:30"
    assert win.undo.isClean() and win.undo.count() == 0


def test_clean_editor_follows_outside_changes_silently(app, win):
    _edit_file(win.path, 'end   = "10:00"', 'end   = "10:30"')
    assert win.check_disk() == "timetable"
    assert win.tt.courses[0].end == "10:30" and win.banner.isHidden()


def test_own_save_is_not_seen_as_an_outside_change(app, win):
    _dirty(win)
    assert win.save(show_summary=False)
    assert win.check_disk() == "same" and win.banner.isHidden()


def test_broken_file_refuses_to_save(app, win, monkeypatch):
    _dirty(win)
    win.path.write_text("[[깨짐", encoding="utf-8")
    assert win.check_disk() == "broken" and win.banner.isVisible()
    _choose(monkeypatch, "cancel")
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)
    assert not win.save(show_summary=False)
    assert win.path.read_text(encoding="utf-8") == "[[깨짐"


def test_file_watcher_notices_a_real_outside_write(app, win):
    import time

    _edit_file(win.path, 'end   = "10:00"', 'end   = "10:40"')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and win.tt.courses[0].end != "10:40":
        app.processEvents()
        time.sleep(0.05)
    assert win.tt.courses[0].end == "10:40"


# -- 창을 작업 가능한 영역 안에 ------------------------------------------
def test_fit_rect_rules():
    from PySide6.QtCore import QRect

    avail = QRect(0, 0, 1920, 1040)                        # 작업 표시줄 40px 뺀 영역
    inside = QRect(100, 100, 800, 600)
    assert gui.fit_rect(inside, avail) == inside           # 안이면 그대로(자리 존중)
    too_tall = QRect(100, 50, 900, 1300)                   # 예전 고정 높이처럼 넘침
    r = gui.fit_rect(too_tall, avail)
    assert r.height() == 1040 - 2 * gui.FIT_MARGIN and r.bottom() <= avail.bottom()
    off = QRect(-300, 900, 800, 600)                       # 왼쪽·아래로 나감
    r = gui.fit_rect(off, avail)
    assert avail.contains(r) and r.size() == off.size()
    second = QRect(1920, 0, 1280, 1024)                    # 두 번째 모니터 영역 기준
    r = gui.fit_rect(QRect(3000, 900, 600, 400), second)
    assert second.contains(r)


def test_start_rect_is_centered_and_capped():
    from PySide6.QtCore import QRect, QSize

    small = QRect(0, 0, 1366, 728)                        # 작은 노트북
    r = gui.start_rect(QSize(792, 900), small, QSize(16, 39))
    assert small.contains(r) and r.height() <= int(728 * gui.FIT_START_RATIO)
    assert abs(r.center().x() - small.center().x()) <= 1
    big = QRect(0, 0, 2560, 1400)
    assert gui.start_rect(QSize(792, 900), big).size() == QSize(792, 900)


def test_window_opens_and_stays_inside_available_area(app, win):
    avail = win.screen().availableGeometry()
    assert avail.contains(win.frameGeometry()), (avail, win.frameGeometry())
    win.move(avail.right() - 50, avail.bottom() - 30)     # 거의 다 밖으로
    app.processEvents()
    assert win.keeper.check() is True
    assert avail.contains(win.frameGeometry())
    assert win.keeper.check() is False                     # 이미 안이면 손대지 않음


def test_keeper_leaves_maximized_windows_alone(app, win):
    win.showMaximized()
    app.processEvents()
    assert win.keeper.check() is False
    win.showNormal()
    app.processEvents()


def test_dialogs_are_kept_inside_too(app, win):
    s = win.show_summary()
    app.processEvents()
    s.move(-2000, -2000)
    app.processEvents()
    gui.ScreenKeeper.attach(s).check()
    assert s.screen().availableGeometry().contains(s.frameGeometry())
    assert gui.ScreenKeeper.attach(s) is gui.ScreenKeeper.attach(s)   # 창마다 하나


# -- 긴 글자: 자리 있으면 창을 넓혀 한 줄, 모자랄 때만 줄바꿈 ------------
def _lines(label):
    return round(label.height() / label.fontMetrics().lineSpacing())


def test_preferred_width_fits_the_hint_on_one_line(app, win):
    want = win._preferred_size()
    assert want.width() >= gui.natural_width(win.hint)
    # 화면이 넉넉하면(가상 큰 화면) 그 폭으로 열리고, 그 폭에서 문구는 한 줄.
    from PySide6.QtCore import QRect
    big = QRect(0, 0, 2560, 1400)
    assert gui.start_rect(want, big).width() == want.width()
    win.keeper.timer.stop()
    win.resize(want.width(), 700)
    app.processEvents()
    win.keeper.timer.stop()
    assert _lines(win.hint) == 1


def test_hint_wraps_only_when_the_area_is_too_narrow(app, win):
    avail = win.screen().availableGeometry()            # 테스트 화면은 800 폭
    assert win._preferred_size().width() > avail.width()
    assert win.frameGeometry().width() <= avail.width()  # 영역 안으로 줄었고
    assert _lines(win.hint) >= 2                         # 그래서 이때만 접힘


def test_long_text_does_not_force_a_wide_minimum(app, win):
    """줄바꿈을 끄면 최소 폭이 950px 로 묶여 작은 화면을 넘었습니다."""
    assert win.minimumSizeHint().width() < 700


# -- eClass 과목 연결 ------------------------------------------------------
from syncnote.eclass import catalog as CAT  # noqa: E402

CATALOG = [CAT.Entry("C001", "운동과건강", "2026-2", "eclass"),
           CAT.Entry("C002", "자료구조및알고리즘", "2026-2", "eclass"),
           CAT.Entry("", "캡스톤디자인", "2026-2", "folder")]


def test_editor_suggests_and_applies_an_eclass_course(app):
    ed = gui.CourseEditor(None, M.Course("", ["mon"]), "t", CATALOG)
    ed.name.setText("건강 운동과")
    assert "운동과건강 [C001]" in ed.suggestion.text() and ed.apply_btn.isEnabled()
    ed.apply_suggestion()
    assert ed.name.text() == "운동과건강" and ed.course_id.text() == "C001"
    assert ed.suggestion.text().startswith("✔ 연결됨") and not ed.apply_btn.isEnabled()
    assert ed.name.completer() is not None


def test_editor_folder_only_course_fills_name_but_not_code(app):
    ed = gui.CourseEditor(None, M.Course("캡스톤", ["mon"]), "t", CATALOG)
    assert "코드 모름" in ed.suggestion.text()
    ed.apply_suggestion()
    assert ed.name.text() == "캡스톤디자인" and ed.course_id.text() == ""


def test_editor_without_catalog_explains_how_to_get_one(app):
    ed = gui.CourseEditor(None, M.Course("자료구조", ["mon"]), "t", [])
    assert "수집" in ed.suggestion.text() and not ed.apply_btn.isEnabled()


def test_link_dialog_prechecks_confident_matches_and_applies_once(app, win, monkeypatch):
    new = win.tt.copy()
    new.courses.append(M.Course("운동 건강과", ["fri"], "13:00", "14:00"))
    new.courses.append(M.Course("양자역학", ["thu"], "13:00", "14:00"))
    win._apply(new, "t")
    win.catalog = CATALOG
    before = win.undo.count()

    def accept(self):
        checked = [self.table.item(r, 0).checkState() == Qt.CheckState.Checked
                   for r in range(self.table.rowCount())]
        assert checked == [True, True, False]       # 자료구조·운동과건강 ✔, 양자역학 ✗
        return QDialog.DialogCode.Accepted
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(gui.LinkDialog, "exec", accept)
    win.link_courses()
    got = {c.name: c.course_id for c in win.tt.courses}
    assert got == {"자료구조및알고리즘": "C002", "운동과건강": "C001", "양자역학": ""}
    assert win.undo.count() == before + 1           # 한 번에 실행 취소
    win.undo.undo()
    assert all(not c.course_id for c in win.tt.courses)


def test_window_reads_catalog_from_configured_data_dir(app, tmp_path, monkeypatch):
    data = tmp_path / "data"
    CAT.save(data / "state", "2026-2", [type("X", (), {"course_id": "C7", "name": "운영체제"})()])
    p = tmp_path / "config.toml"
    p.write_text(f'[general]\ndata_dir = "{data.as_posix()}"\n\n[eclass]\n'
                 '[[eclass.course_repos]]\nid = "C8"\nname = "컴퓨터구조"\nrepo = "ca"\n',
                 encoding="utf-8")
    w = gui.TimetableWindow(p)
    try:
        assert [(e.id, e.name) for e in w.catalog] == [("C7", "운영체제"), ("C8", "컴퓨터구조")]
    finally:
        w.undo.setClean()
        w.close()


def test_editor_suggestion_one_line_when_room_wraps_when_narrow(app):
    ed = gui.CourseEditor(None, M.Course("건강 운동과", ["fri"]), "t", CATALOG)
    ed.dlg.show()
    app.processEvents()
    app.processEvents()
    lab = ed.suggestion
    # 한 줄 높이는 글꼴 줄 간격과 다를 수 있습니다(한글 대체 글꼴이 더 큼).
    # 아주 넓을 때의 높이 = 한 줄 높이로 보고 비교합니다.
    one_line = lab.heightForWidth(100_000)
    assert lab.heightForWidth(lab.width()) == one_line     # 자리 있으면 한 줄
    ed.dlg.resize(300, ed.dlg.height())
    app.processEvents()
    app.processEvents()
    assert lab.heightForWidth(lab.width()) > one_line      # 좁으면 접히고
    assert lab.minimumHeight() >= lab.heightForWidth(lab.width())   # 높이가 따라 늚
    ed.dlg.close()


# -- config.toml 이 없을 때: 예시 파일로 설정 만들기 -----------------------
from pathlib import Path as _Path  # noqa: E402

EXAMPLE_PATH = _Path(__file__).resolve().parents[1] / "config.example.toml"


def _wizard(tmp_path):
    from syncnote.timetable.config_wizard import NewConfigDialog
    return NewConfigDialog(None, EXAMPLE_PATH, tmp_path / "config.toml")


def test_wizard_lists_every_example_setting_and_starts_with_what_needs_checking(app, tmp_path):
    from syncnote import settings_schema as S

    d = _wizard(tmp_path)
    want = sum(len(s.fields) for s in S.parse(EXAMPLE_PATH.read_text(encoding="utf-8")))
    assert len(d.fields) == want
    assert d.tree.currentItem() is d.tree.topLevelItem(0)          # 먼저 확인할 항목
    first = [f.dotted for f in d._first_fields()]
    assert set(first) == {f.dotted for p in d.problem_list for f in S.fields_in(p, d.fields)}
    assert set(d.rows) == set(first)
    d.close()


def test_wizard_editing_fixes_problems_and_writes_only_changes(app, tmp_path):
    d = _wizard(tmp_path)
    d.tree.setCurrentItem(d.tree.topLevelItem(0))
    row = d.rows.get("eclass.username")
    if row is not None:                         # 예시가 학번을 비워 둔 경우
        row.editor.setText("20261234")
        d.revalidate()
        assert not any("eclass.username" in p for p in d.problem_list)
    # 다른 분류의 숫자·체크·목록도
    d.search.setText("retry_count")
    d.rows["github.retry_count"].editor.setValue(9)
    d.search.setText("message_boxes")
    d.rows["eclass.message_boxes"].editor.setPlainText("RECEIVE\nSEND")
    d.revalidate()
    text = d.result_text()
    data = tomllib_loads(text)
    assert data["github"]["retry_count"] == 9
    assert data["eclass"]["message_boxes"] == ["RECEIVE", "SEND"]
    assert "# 로그 수준" in text                                     # 예시 주석 유지
    d.search.clear()
    d.only_changed.setChecked(True)
    assert "github.retry_count" in d.rows and "general.log_level" not in d.rows
    d.rows["github.retry_count"].reset()                               # 기본값으로
    d.revalidate()
    assert "github.retry_count" not in d.changes()
    d.close()


def tomllib_loads(text):
    import sys
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    return tomllib.loads(text)


def test_wizard_bad_list_value_is_flagged_not_written(app, tmp_path):
    d = _wizard(tmp_path)
    d.search.setText("recording.video.monitors")
    rows = [r for k, r in d.rows.items() if k == "recording.video.monitors"]
    assert rows and rows[0].field.kind == "list"
    rows[0].editor.setPlainText("하나\n2")             # 모니터 번호 목록에 글자
    d.revalidate()
    assert d.changes()["recording.video.monitors"] == ["하나", 2]    # 2 는 숫자로 읽음
    assert any("recording.video.monitors" in p for p in d.problem_list)   # 실제 검증이 잡음
    assert d.rows["recording.video.monitors"].note.isVisibleTo(d)
    d.close()


def _new_window(tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Discard)
    w = gui.TimetableWindow(tmp_path / "config.toml")
    new = w.tt.copy()
    new.courses.append(M.Course("자료구조", ["mon"], "09:00", "10:00"))
    w._apply(new, "t")
    return w


def test_saving_without_config_asks_settings_first_then_writes_full_config(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from syncnote import config as config_module
    from syncnote.timetable.config_wizard import NewConfigDialog

    def fill_and_accept(self):
        self.values["eclass.username"] = "20261234"
        self.values["general.data_dir"] = str(tmp_path / "data")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(NewConfigDialog, "exec", fill_and_accept)
    w = _new_window(tmp_path, monkeypatch)
    try:
        assert w.save(show_summary=False)
        text = w.path.read_text(encoding="utf-8")
        assert text.startswith(EXAMPLE_PATH.read_text(encoding="utf-8")[:40])   # 예시 본문 그대로
        assert 'username = "20261234"' in text and M.BEGIN in text            # 설정 + 시간표
        cfg = config_module.load(w.path)                                       # 실제 검증 통과
        assert cfg.get("eclass.username") == "20261234"
        assert [r["name"] for r in cfg.get("recording.timetable")] == ["자료구조"]
    finally:
        w.undo.setClean()
        w.close()


def test_cancelling_settings_does_not_create_the_file(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from syncnote.timetable.config_wizard import NewConfigDialog

    monkeypatch.setattr(NewConfigDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    w = _new_window(tmp_path, monkeypatch)
    try:
        assert not w.save(show_summary=False)
        assert not w.path.exists() and not w.undo.isClean()
    finally:
        w.undo.setClean()
        w.close()


def test_no_example_anywhere_falls_back_to_timetable_only_after_asking(app, tmp_path, monkeypatch):
    w = _new_window(tmp_path, monkeypatch)
    monkeypatch.setattr(gui.TimetableWindow, "_example_path", lambda self: None)
    asked = {}

    def yes(*a, **k):
        asked["text"] = a[2]
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", yes)
    try:
        assert w.save(show_summary=False)
        assert "config.example.toml" in asked["text"] and M.load(w.path).courses
    finally:
        w.undo.setClean()
        w.close()
