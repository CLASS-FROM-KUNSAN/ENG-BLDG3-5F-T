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
    w = gui.TimetableWindow(p)
    w.resize(1000, 1400)
    w.show()
    app.processEvents()
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
