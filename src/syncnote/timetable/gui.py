"""PySide6 시간표 편집기.

    python -m syncnote timetable edit [-c config.toml]

Swift 앱(2026년 시간표)의 화면 구성을 데스크톱으로 옮겼습니다.
  - 시간표 탭: 주간 격자. 빈 칸을 드래그하면 수업 추가, 블록을 끌면 이동,
    아래 가장자리를 끌면 길이 조절, 더블클릭하면 수정. 5분 단위로 맞춥니다.
    Shift 를 누른 채 끌면 그 요일만 떼어 옮깁니다(다른 요일은 그대로).
  - 수업 목록 탭: 표로 보고 추가/수정/복제/삭제.
  - 공휴일 탭: 쉬는 날. 녹화·송출 일정에서 건너뜁니다. 붙여넣기 일괄 추가.
  - 저장 = config.toml 의 [[recording.timetable]] / [[recording.holidays]].
  - Swift 앱 백업 JSON 가져오기/내보내기.

수업 추가/수정 창은 course_dialog.ui 로, Qt Designer 로 고칠 수 있습니다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import shiboken6
from PySide6.QtCore import (
    QDate,
    QEvent,
    QFile,
    QFileSystemWatcher,
    QIODevice,
    QLibraryInfo,
    QLocale,
    QRectF,
    Qt,
    QTime,
    QTimer,
    QTranslator,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QUndoCommand,
    QUndoStack,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import model as M

# 격자 크기 (1분 = 1px)
TIME_W = 52
DAY_W = 132
HEADER_H = 30
PX_PER_MIN = 1.0
SNAP = 5
MIN_LEN = 10
RESIZE_GRIP = 7


def snap(minute: float) -> int:
    return int(round(minute / SNAP) * SNAP)


def _text_color(bg: QColor) -> QColor:
    # 밝은 블록 위에는 검정 글씨. (YIQ 휘도)
    y = (bg.red() * 299 + bg.green() * 587 + bg.blue() * 114) / 1000
    return QColor("#1B1B1B") if y > 170 else QColor("#FFFFFF")


# ---------------------------------------------------------------------------
# 실행 취소: 통째 스냅샷 (시간표는 작아서 충분히 쌉니다)
# ---------------------------------------------------------------------------
class _Snapshot(QUndoCommand):
    def __init__(self, win: TimetableWindow, before: M.Timetable,
                 after: M.Timetable, text: str) -> None:
        super().__init__(text)
        self._win, self._before, self._after = win, before, after

    def redo(self) -> None:
        self._win._set_model(self._after.copy())

    def undo(self) -> None:
        self._win._set_model(self._before.copy())


# ---------------------------------------------------------------------------
# 시간표 격자
# ---------------------------------------------------------------------------
class CourseBlock(QGraphicsRectItem):
    def __init__(self, scene: TimetableScene, idx: int, course: M.Course, day: str,
                 rect: QRectF, warn: bool) -> None:
        super().__init__(rect)
        self.grid = scene
        self.idx, self.course, self.day = idx, course, day
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setZValue(10)
        bg = QColor(course.color)
        bg.setAlpha(225)
        self.setBrush(QBrush(bg))
        self.setPen(QPen(QColor("#C0392B"), 2.5) if warn else QPen(bg.darker(130), 1))
        tip = f"{course.name}\n{course.days_text}  {course.time_text}"
        if course.room:
            tip += f"\n{course.room}"
        if warn:
            tip += "\n⚠ 같은 요일의 다른 수업과 시간이 겹칩니다"
        self.setToolTip(tip)

        # 참조를 붙잡아 둡니다. 지역 변수로만 두면 PySide 가 Python 쪽 객체를
        # 정리하면서 자식 아이템이 같이 사라져 블록에 글자가 안 나옵니다.
        self.label = label = QGraphicsTextItem(self)
        label.setDefaultTextColor(_text_color(bg))
        lines = [f"<b>{_esc(course.name)}</b>"]
        if rect.height() >= 44 and course.room:
            lines.append(_esc(course.room))
        if rect.height() >= 62:
            lines.append(course.time_text)
        label.setHtml("<br>".join(lines))
        f = QFont()
        f.setPointSizeF(8.5)
        label.setFont(f)
        label.setTextWidth(rect.width() - 6)
        label.setPos(rect.x() + 2, rect.y() + 1)
        self._mode = ""
        self._press = None
        self._orig = QRectF(rect)

    # -- 마우스 ---------------------------------------------------------
    def _near_bottom(self, y: float) -> bool:
        return self.rect().bottom() - y <= RESIZE_GRIP

    def hoverMoveEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.SizeVerCursor if self._near_bottom(event.pos().y())
                       else Qt.CursorShape.OpenHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._mode = "resize" if self._near_bottom(event.pos().y()) else "move"
        self._press = event.scenePos()
        self._orig = QRectF(self.rect())
        self.setZValue(20)
        self.setOpacity(0.8)
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._mode:
            super().mouseMoveEvent(event)
            return
        d = event.scenePos() - self._press
        r = QRectF(self._orig)
        if self._mode == "resize":
            r.setHeight(max(MIN_LEN * PX_PER_MIN, self._orig.height() + d.y()))
            self.setRect(r)
        else:
            self.setPos(d.x(), d.y())

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._mode:
            super().mouseReleaseEvent(event)
            return
        mode, self._mode = self._mode, ""
        g = self.grid
        if mode == "resize":
            start = self.course.start_min
            grown = (self.rect().height() - self._orig.height()) / PX_PER_MIN
            end = max(start + MIN_LEN, snap(self.course.end_min + grown))
            end = min(end, 24 * 60 - 1)
            to_day = self.day
        else:
            moved = self.mapRectToScene(self.rect())
            to_day = g.day_at(moved.center().x()) or self.day
            length = self.course.end_min - self.course.start_min
            start = snap(g.minute_at(moved.top()))
            start = max(0, min(start, 24 * 60 - 1 - length))
            end = start + length
        only_this = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        g.moveRequested.emit(self.idx, self.day, to_day, start, end, only_this)

    def mouseDoubleClickEvent(self, event) -> None:
        self.grid.editRequested.emit(self.idx)

    def contextMenuEvent(self, event) -> None:
        self.grid.contextRequested.emit(self.idx, self.day, event.screenPos())


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    """a 에서 b 쪽으로 t 만큼(0=a, 1=b)."""
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


@dataclass(frozen=True)
class Theme:
    """격자 색. 고정색 대신 지금 시스템 팔레트(라이트/다크)에서 뽑습니다."""
    bg: QColor
    text: QColor
    dim: QColor
    grid: QColor
    half: QColor
    weekend: QColor
    accent: QColor
    dark: bool

    @classmethod
    def current(cls) -> Theme:
        app = QApplication.instance()
        pal = app.palette() if app is not None else QPalette()
        bg = pal.color(QPalette.ColorRole.Base)
        text = pal.color(QPalette.ColorRole.Text)
        dark = bg.lightness() < 128
        return cls(
            bg=bg, text=text,
            dim=_mix(text, bg, 0.45),
            grid=_mix(text, bg, 0.82 if dark else 0.88),
            half=_mix(text, bg, 0.90 if dark else 0.94),
            weekend=_mix(text, bg, 0.96 if dark else 0.98),
            # 강조색(오늘 요일)은 다크 배경에서 어두우면 안 보여 밝게 올립니다.
            accent=(pal.color(QPalette.ColorRole.Highlight).lighter(160) if dark
                    else pal.color(QPalette.ColorRole.Highlight)),
            dark=dark,
        )


def _lanes(items: list[tuple[int, M.Course]]) -> list[tuple[int, M.Course, int, int]]:
    """같은 요일에서 시간이 겹치는 수업을 나란히 그리기 위한 칸 배정.

    겹치는 무리(cluster)마다 필요한 칸 수를 따로 셉니다. 겹치지 않는
    수업은 칸 하나를 통째로 씁니다. 반환: (idx, course, 칸 번호, 칸 수).
    """
    items = sorted(items, key=lambda t: (t[1].start_min, t[1].end_min))
    out: list[tuple[int, M.Course, int, int]] = []
    cluster: list[tuple[int, M.Course, int]] = []
    lane_ends: list[int] = []
    cluster_end = -1

    def flush() -> None:
        n = max(1, len(lane_ends))
        out.extend((i, c, lane, n) for i, c, lane in cluster)

    for idx, c in items:
        if cluster and c.start_min >= cluster_end:
            flush()
            cluster, lane_ends = [], []
        lane = next((k for k, end in enumerate(lane_ends) if end <= c.start_min), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(c.end_min)
        else:
            lane_ends[lane] = c.end_min
        cluster.append((idx, c, lane))
        cluster_end = max(cluster_end, c.end_min) if len(cluster) > 1 else c.end_min
    if cluster:
        flush()
    return out


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class TimetableScene(QGraphicsScene):
    # idx, from_day, to_day, start, end, only_this_day
    moveRequested = Signal(int, str, str, int, int, bool)
    createRequested = Signal(str, int, int)
    editRequested = Signal(int)
    contextRequested = Signal(int, str, object)

    def __init__(self) -> None:
        super().__init__()
        self.days: list[str] = list(M.DAYS[:5])
        self.h0, self.h1 = 8, 21
        self._creating: tuple[str, int] | None = None
        self._rubber: QGraphicsRectItem | None = None
        self.theme = Theme.current()

    # -- 좌표 -----------------------------------------------------------
    def x_of(self, day: str) -> float:
        return TIME_W + self.days.index(day) * DAY_W

    def y_of(self, minute: float) -> float:
        return HEADER_H + (minute - self.h0 * 60) * PX_PER_MIN

    def minute_at(self, y: float) -> float:
        return (y - HEADER_H) / PX_PER_MIN + self.h0 * 60

    def day_at(self, x: float) -> str | None:
        col = int((x - TIME_W) // DAY_W)
        return self.days[col] if 0 <= col < len(self.days) else None

    def _in_grid(self, pos) -> bool:
        return (TIME_W <= pos.x() < TIME_W + DAY_W * len(self.days)
                and HEADER_H <= pos.y() <= self.y_of(self.h1 * 60))

    # -- 그리기 ---------------------------------------------------------
    def rebuild(self, tt: M.Timetable, show_weekend: bool) -> None:
        self.clear()
        self._rubber = None
        used = {d for c in tt.courses for d in c.days}
        self.days = [d for d in M.DAYS
                     if M.DAY_INDEX[d] < 5 or show_weekend or d in used]
        valid = [c for c in tt.courses if not c.problems()]
        lo = min([c.start_min for c in valid], default=9 * 60)
        hi = max([c.end_min for c in valid], default=18 * 60)
        self.h0 = max(0, min(8, lo // 60))
        self.h1 = min(24, max(21, -(-hi // 60)))
        width = TIME_W + DAY_W * len(self.days)
        bottom = self.y_of(self.h1 * 60)

        th = self.theme = Theme.current()
        self.setBackgroundBrush(QBrush(th.bg))
        grid_pen = QPen(th.grid)
        half_pen = QPen(th.half)
        head_font = QFont()
        head_font.setBold(True)
        today = date.today().weekday()
        for i, d in enumerate(self.days):
            x = TIME_W + i * DAY_W
            if M.DAY_INDEX[d] >= 5:
                self.addRect(QRectF(x, HEADER_H, DAY_W, bottom - HEADER_H),
                             QPen(Qt.PenStyle.NoPen), QBrush(th.weekend))
            t = self.addText(M.DAY_KR[M.DAY_INDEX[d]], head_font)
            # 색을 안 주면 시스템 글자색을 씁니다. 다크 모드에서는 흰 글자라
            # 예전처럼 배경이 흰색 고정이면 요일이 사라졌습니다.
            t.setDefaultTextColor(th.accent if M.DAY_INDEX[d] == today else th.text)
            t.setPos(x + DAY_W / 2 - t.boundingRect().width() / 2, 4)
            self.addLine(x, HEADER_H, x, bottom, grid_pen)
        self.addLine(width, HEADER_H, width, bottom, grid_pen)
        for h in range(self.h0, self.h1 + 1):
            y = self.y_of(h * 60)
            self.addLine(TIME_W, y, width, y, grid_pen)
            if h < self.h1:
                self.addLine(TIME_W, y + 30 * PX_PER_MIN, width, y + 30 * PX_PER_MIN, half_pen)
                lab = self.addText(f"{h}:00")
                lab.setDefaultTextColor(th.dim)
                lab.setPos(TIME_W - lab.boundingRect().width() - 4, y - 9)

        warn = {id(c) for _d, a, b in tt.overlaps() for c in (a, b)}
        for d in self.days:
            todays = [(idx, c) for idx, c in enumerate(tt.courses)
                      if d in c.days and not c.problems()]
            for idx, c, lane, lanes in _lanes(todays):
                w = (DAY_W - 4) / lanes
                r = QRectF(self.x_of(d) + 2 + lane * w, self.y_of(c.start_min) + 1,
                           w - (1 if lanes > 1 else 0),
                           (c.end_min - c.start_min) * PX_PER_MIN - 2)
                self.addItem(CourseBlock(self, idx, c, d, r, id(c) in warn))
        self.setSceneRect(QRectF(0, 0, width + 8, bottom + 8))

    # -- 빈 칸 드래그로 추가 --------------------------------------------
    def _block_at(self, pos) -> bool:
        for item in self.items(pos):
            while item is not None:
                if isinstance(item, CourseBlock):
                    return True
                item = item.parentItem()
        return False

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        pos = event.scenePos()
        if (event.button() == Qt.MouseButton.LeftButton and not self._block_at(pos)
                and self._in_grid(pos)):
            day = self.day_at(pos.x())
            m = snap(self.minute_at(pos.y()))
            self._creating = (day, m)
            self._rubber = self.addRect(
                QRectF(self.x_of(day) + 2, self.y_of(m), DAY_W - 4, 1),
                QPen(QColor("#4A90D9"), 1, Qt.PenStyle.DashLine),
                QBrush(QColor(74, 144, 217, 60)))
            self._rubber.setZValue(30)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._creating and self._rubber is not None:
            day, m0 = self._creating
            m1 = snap(self.minute_at(event.scenePos().y()))
            top, bot = sorted((m0, m1))
            self._rubber.setRect(QRectF(self.x_of(day) + 2, self.y_of(top), DAY_W - 4,
                                        max(1, (bot - top) * PX_PER_MIN)))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._creating:
            day, m0 = self._creating
            m1 = snap(self.minute_at(event.scenePos().y()))
            self._creating = None
            if self._rubber is not None:
                self.removeItem(self._rubber)
                self._rubber = None
            top, bot = sorted((m0, m1))
            if bot - top >= MIN_LEN:
                self.createRequested.emit(day, top, bot)
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        pos = event.scenePos()
        if not self._block_at(pos) and self._in_grid(pos):
            m = snap(self.minute_at(pos.y()))
            self.createRequested.emit(self.day_at(pos.x()), m, min(m + 60, 24 * 60 - 1))
            return
        super().mouseDoubleClickEvent(event)


# ---------------------------------------------------------------------------
# 수업 추가/수정 창 (course_dialog.ui)
# ---------------------------------------------------------------------------
def load_ui(name: str, parent: QWidget | None = None) -> QWidget:
    path = Path(__file__).with_name(name)
    # Qt 장치(QFile)는 바이트를 그대로 QUiLoader 에 넘깁니다(텍스트 디코딩 없음).
    ui_file = QFile(str(path))
    if not ui_file.open(QIODevice.OpenModeFlag.ReadOnly):
        raise FileNotFoundError(path)
    try:
        w = QUiLoader().load(ui_file, parent)
    finally:
        ui_file.close()
    if w is None:
        raise RuntimeError(f"{name} 를 불러오지 못했습니다")
    return w


_DAY_WIDGETS = ("dayMon", "dayTue", "dayWed", "dayThu", "dayFri", "daySat", "daySun")


class CourseEditor:
    """course_dialog.ui 를 감싸 Course 를 주고받습니다."""

    def __init__(self, parent: QWidget | None, course: M.Course, title: str) -> None:
        self.dlg: QDialog = load_ui("course_dialog.ui", parent)
        self.dlg.setWindowTitle(title)
        w = self.dlg.findChild
        self.name: QLineEdit = w(QLineEdit, "nameEdit")
        self.professor: QLineEdit = w(QLineEdit, "professorEdit")
        self.room: QLineEdit = w(QLineEdit, "roomEdit")
        self.course_id: QLineEdit = w(QLineEdit, "courseIdEdit")
        self.language: QComboBox = w(QComboBox, "languageCombo")
        self.days = [w(QCheckBox, n) for n in _DAY_WIDGETS]
        self.start: QTimeEdit = w(QTimeEdit, "startTime")
        self.end: QTimeEdit = w(QTimeEdit, "endTime")
        self.preview: QLabel = w(QLabel, "previewLabel")
        self.error: QLabel = w(QLabel, "errorLabel")
        grid: QGridLayout = self.dlg.findChild(QGridLayout, "colorGrid")
        buttons: QDialogButtonBox = w(QDialogButtonBox, "buttonBox")

        self.color = course.color
        self._swatches: list[QToolButton] = []
        for i, hex_ in enumerate(M.PALETTE):
            b = QToolButton()
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFixedSize(30, 30)
            b.setToolTip(hex_)
            b.setStyleSheet(f"QToolButton {{ background: {hex_}; border-radius: 15px; "
                            f"border: 2px solid white; }} QToolButton:checked {{ "
                            f"border: 3px solid #222; }}")
            b.clicked.connect(lambda _=False, h=hex_: self._pick(h))
            grid.addWidget(b, i // 6, i % 6)
            self._swatches.append(b)
        other = QPushButton("기타…")
        other.clicked.connect(self._pick_custom)
        grid.addWidget(other, len(M.PALETTE) // 6, 0, 1, 2)

        self.name.setText(course.name)
        self.professor.setText(course.professor)
        self.room.setText(course.room)
        self.course_id.setText(course.course_id)
        self.language.setCurrentText(course.language)
        for cb, d in zip(self.days, M.DAYS, strict=True):
            cb.setChecked(d in course.days)
        self.start.setTime(QTime(course.start_min // 60, course.start_min % 60))
        self.end.setTime(QTime(course.end_min // 60, course.end_min % 60))
        self._pick(course.color)

        for sig in (self.name.textChanged, self.room.textChanged,
                    self.language.currentTextChanged, self.start.timeChanged,
                    self.end.timeChanged):
            sig.connect(self._refresh)
        for cb in self.days:
            cb.toggled.connect(self._refresh)
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.dlg.reject)
        self._refresh()

    def _pick(self, hex_: str) -> None:
        self.color = hex_.upper()
        for b in self._swatches:
            b.setChecked(b.toolTip().upper() == self.color)
        self._refresh()

    def _pick_custom(self) -> None:
        c = QColorDialog.getColor(QColor(self.color), self.dlg, "블록 색상")
        if c.isValid():
            self._pick(c.name())

    def course(self) -> M.Course:
        return M.Course(
            name=self.name.text().strip(),
            days=[d for cb, d in zip(self.days, M.DAYS, strict=True) if cb.isChecked()],
            start=self.start.time().toString("HH:mm"),
            end=self.end.time().toString("HH:mm"),
            professor=self.professor.text().strip(),
            room=self.room.text().strip(),
            color=self.color,
            language=self.language.currentText().strip(),
            course_id=self.course_id.text().strip(),
        )

    def _refresh(self, *_a) -> None:
        c = self.course()
        mins = c.end_min - c.start_min
        lang = c.language or "기본([transcribe])"
        where = f" · {c.room}" if c.room else ""
        self.preview.setText(
            f"녹화: {c.days_text or '요일 없음'}  {c.time_text}"
            f" ({mins}분){where}\n전사 언어: {lang}")
        self.error.setText("")

    def _try_accept(self) -> None:
        problems = self.course().problems()
        if problems:
            self.error.setText("\n".join(problems))
            return
        self.dlg.accept()

    def exec(self) -> M.Course | None:
        return self.course() if self.dlg.exec() == QDialog.DialogCode.Accepted else None


# ---------------------------------------------------------------------------
# 공휴일 창들
# ---------------------------------------------------------------------------
def ask_holiday(parent: QWidget, existing: M.Holiday | None = None) -> M.Holiday | None:
    """공휴일 추가(existing=None) 또는 수정 창."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("공휴일 수정" if existing else "공휴일 추가")
    form = QFormLayout(dlg)
    start = (QDate(existing.day.year, existing.day.month, existing.day.day)
             if existing else QDate.currentDate())
    when = QDateEdit(start)
    when.setCalendarPopup(True)
    when.setDisplayFormat("yyyy-MM-dd (ddd)")
    name = QLineEdit(existing.name if existing else "")
    name.setPlaceholderText("예: 개교기념일")
    form.addRow("날짜", when)
    form.addRow("이름", name)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    form.addRow(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    q = when.date()
    return M.Holiday(date(q.year(), q.month(), q.day()), name.text().strip())


def ask_holiday_paste(parent: QWidget) -> list[M.Holiday]:
    dlg = QDialog(parent)
    dlg.setWindowTitle("붙여넣기로 공휴일 추가")
    dlg.resize(460, 480)
    lay = QVBoxLayout(dlg)
    row = QHBoxLayout()
    row.addWidget(QLabel("연도"))
    year = QSpinBox()
    year.setRange(2000, 2100)
    year.setValue(date.today().year)
    row.addWidget(year)
    row.addStretch(1)
    lay.addLayout(row)
    lay.addWidget(QLabel("한 줄에 하나씩. 날짜(월/일)만 있으면 앞뒤 글자는 이름이 됩니다.\n"
                         "예) 3월 1일(일) 삼일절 · 5/5 어린이날 · 2026-10-09 한글날"))
    text = QPlainTextEdit()
    lay.addWidget(text, 1)
    preview = QLabel()
    preview.setWordWrap(True)
    lay.addWidget(preview)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    ok = bb.button(QDialogButtonBox.StandardButton.Ok)
    lay.addWidget(bb)
    found: list[M.Holiday] = []

    def refresh() -> None:
        found[:] = M.parse_holiday_text(text.toPlainText(), year.value())
        ok.setEnabled(bool(found))
        if found:
            preview.setText(f"인식된 날짜 {len(found)}개: " + ", ".join(
                f"{h.day:%m/%d} {h.name}".strip() for h in found[:12])
                + (" …" if len(found) > 12 else ""))
        else:
            preview.setText("인식된 날짜가 없습니다.")

    text.textChanged.connect(refresh)
    year.valueChanged.connect(refresh)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    refresh()
    return list(found) if dlg.exec() == QDialog.DialogCode.Accepted else []


# ---------------------------------------------------------------------------
# 저장 결과 · 다가오는 일정
# ---------------------------------------------------------------------------
def _fmt_when(u: M.Upcoming) -> str:
    return f"{u.start:%m/%d}({M.DAY_KR[u.start.weekday()]}) {u.start:%H:%M}~{u.end:%H:%M}"


class SaveSummary(QDialog):
    """저장 직후 보여 주는 창.

    - 다음 녹화: 저장된 상태면 **config.toml 을 다시 읽어** 녹화기와 같은
      방식으로 계산합니다. 편집기에 보이는 것과 녹화기가 읽는 것이 같은지
      그 자리에서 확인하는 용도입니다. 공휴일로 건너뛰는 회차도 보여 줍니다.
    - 다가오는 공휴일: 여기서 바로 추가·수정·삭제. 고치면 일정이 다시
      계산되고, 저장 버튼이 켜집니다(실행 취소도 그대로 됩니다).
    """

    COUNT = 8

    def __init__(self, win: TimetableWindow) -> None:
        super().__init__(win)
        self.win = win
        self.setWindowTitle("저장 결과 · 다가오는 일정")
        self.resize(520, 560)
        lay = QVBoxLayout(self)

        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)

        lay.addWidget(QLabel(f"<b>다음 녹화</b> (앞으로 {self.COUNT}회)"))
        self.next_list = QListWidget()
        lay.addWidget(self.next_list, 3)

        lay.addWidget(QLabel("<b>다가오는 공휴일</b> — 이 날의 수업은 녹화·송출하지 않습니다"))
        self.htable = QTableWidget(0, 2)
        self.htable.setHorizontalHeaderLabels(["날짜", "이름"])
        self.htable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.htable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.htable.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.htable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.htable.verticalHeader().setVisible(False)
        self.htable.cellDoubleClicked.connect(
            lambda r, _c: win.edit_holiday(win._holiday_at(self.htable, r)))
        lay.addWidget(self.htable, 2)

        row = QHBoxLayout()
        self.add_btn = QPushButton("추가")
        self.add_btn.clicked.connect(win.add_holiday)
        self.edit_btn = QPushButton("수정")
        self.edit_btn.clicked.connect(
            lambda: win.edit_holiday(win._selected_holiday(self.htable)))
        self.del_btn = QPushButton("삭제")
        self.del_btn.clicked.connect(lambda: win.delete_holiday_days(
            {win._holiday_at(self.htable, i.row())
             for i in self.htable.selectionModel().selectedRows()}))
        for b in (self.add_btn, self.edit_btn, self.del_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.save_btn = QPushButton("저장")
        self.save_btn.clicked.connect(lambda: win.save(show_summary=False))
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        bottom.addWidget(self.save_btn)
        bottom.addWidget(close)
        lay.addLayout(bottom)

        # 편집기에서 무엇을 바꾸든(여기 버튼, 탭, 실행 취소) 다시 계산.
        # 람다가 아니라 이 창의 메서드에 연결해야, 창이 사라질 때 Qt 가 연결도
        # 같이 끊습니다(람다면 닫히는 중에 지워진 undo 스택을 건드림).
        win.undo.indexChanged.connect(self._on_undo)
        win.undo.cleanChanged.connect(self._on_undo)
        self.htable.itemSelectionChanged.connect(self._update_buttons)

    def _on_undo(self, *_a) -> None:
        if self.isVisible():
            self.refresh()

    def refresh(self, now: datetime | None = None) -> None:
        win = self.win
        if not (shiboken6.isValid(win) and shiboken6.isValid(win.undo)):
            return
        clean = win.undo.isClean()
        if clean:
            try:
                rows, hrows = M.saved_rows(win.path)
                source = f"{win.path.name} 을 다시 읽어 녹화기와 같은 방식으로 계산했습니다."
            except Exception as exc:  # noqa: BLE001 — 읽기 실패도 화면에 보여 줌
                rows, hrows = win.tt.to_rows(), win.tt.holiday_rows()
                source = f"⚠ {win.path.name} 을 다시 읽지 못했습니다: {exc}"
            self.state.setText(f"✔ 저장됨 — {win.path.name}\n{source}")
            self.state.setToolTip(str(win.path))
        else:
            rows, hrows = win.tt.to_rows(), win.tt.holiday_rows()
            self.state.setText("● 저장하지 않은 변경이 있습니다. 아래는 저장 전 내용으로 계산한 "
                               "것이며, 저장해야 녹화에 반영됩니다.")
        self.save_btn.setEnabled(not clean)

        self.next_list.clear()
        items = M.upcoming(rows, hrows, now=now, count=self.COUNT)
        dim = Theme.current().dim
        for u in items:
            if u.skipped:
                it = QListWidgetItem(f"{_fmt_when(u)}  {u.name} · 쉼({u.skipped_for or '공휴일'})")
                it.setToolTip(f"{u.skipped_for or '공휴일'} 이라 이 회차는 녹화·송출하지 않습니다")
                it.setForeground(QBrush(dim))
                f = it.font()
                f.setStrikeOut(True)
                it.setFont(f)
            else:
                it = QListWidgetItem(f"{_fmt_when(u)}  {u.name}")
            self.next_list.addItem(it)
        if not items:
            self.next_list.addItem("예정된 수업이 없습니다. 시간표에 수업을 추가하세요.")

        today = (now or datetime.now()).date()
        hs = sorted((h for h in win.tt.holidays if h.day >= today), key=lambda h: h.day)
        self.htable.setRowCount(len(hs))
        for r, h in enumerate(hs):
            dt = QTableWidgetItem(f"{h.day.isoformat()} ({M.DAY_KR[h.day.weekday()]})")
            dt.setData(Qt.ItemDataRole.UserRole, h.day.isoformat())
            self.htable.setItem(r, 0, dt)
            self.htable.setItem(r, 1, QTableWidgetItem(h.name))
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = bool(self.htable.selectionModel().selectedRows())
        self.edit_btn.setEnabled(has)
        self.del_btn.setEnabled(has)


# ---------------------------------------------------------------------------
# 메인 창
# ---------------------------------------------------------------------------
class TimetableWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.path = Path(config_path)
        self.tt = M.Timetable()
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(lambda _c: self._update_title())
        self.show_weekend = False
        self.summary: SaveSummary | None = None

        self.scene = TimetableScene()
        q = Qt.ConnectionType.QueuedConnection   # 블록 이벤트 안에서 장면을 지우지 않도록
        self.scene.moveRequested.connect(self._on_move, q)
        self.scene.createRequested.connect(self._on_create, q)
        self.scene.editRequested.connect(self.edit_course, q)
        self.scene.contextRequested.connect(self._on_context, q)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_grid_tab(), "시간표")
        self.tabs.addTab(self._build_list_tab(), "수업 목록")
        self.tabs.addTab(self._build_holiday_tab(), "공휴일")
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.addWidget(self._build_banner())
        cl.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self._build_menus()
        self.resize(TIME_W + DAY_W * 5 + 80, 900)

        # 다른 곳(메모장, 다른 PC 동기화, 두 번째 편집기)에서 config.toml 이
        # 바뀌는 것을 감시합니다. 저장은 임시 파일을 바꿔치기(os.replace)
        # 하므로 파일 감시가 끊길 수 있어 폴더도 같이 보고, 매번 다시 겁니다.
        self._disk = M.DiskState(None, ())
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._on_disk_event)
        self.watcher.directoryChanged.connect(self._on_disk_event)
        self._disk_timer = QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(300)     # 저장 도중의 중간 상태를 피하려고 잠깐 기다림
        self._disk_timer.timeout.connect(self.check_disk)

        self.reload_from_disk()

    # -- 다른 곳에서 바뀐 파일 -------------------------------------------
    def _build_banner(self) -> QWidget:
        self.banner = QWidget()
        self.banner.setObjectName("diskBanner")
        self.banner.setStyleSheet("#diskBanner { background: #FFF4CE; } "
                                  "#diskBanner QLabel { color: #5C4400; }")
        lay = QHBoxLayout(self.banner)
        lay.setContentsMargins(10, 6, 10, 6)
        self.banner_label = QLabel()
        self.banner_label.setWordWrap(True)
        lay.addWidget(self.banner_label, 1)
        self.banner_reload = QPushButton("디스크 내용 다시 읽기")
        self.banner_reload.setToolTip("편집기에서 바꾼 것은 버리고 파일의 시간표를 불러옵니다")
        self.banner_reload.clicked.connect(lambda: self._reload_discarding())
        self.banner_keep = QPushButton("내 변경 유지")
        self.banner_keep.setToolTip("띠를 닫습니다. 저장할 때 덮어쓸지 다시 묻습니다")
        self.banner_keep.clicked.connect(lambda: self.banner.hide())
        lay.addWidget(self.banner_reload)
        lay.addWidget(self.banner_keep)
        self.banner.hide()
        return self.banner

    def _watch(self) -> None:
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        if self.path.parent.exists():
            self.watcher.addPath(str(self.path.parent))
        if self.path.exists():
            self.watcher.addPath(str(self.path))

    def _remember_disk(self) -> None:
        self._disk = M.DiskState.read(self.path)
        self.banner.hide()
        self._watch()

    def _on_disk_event(self, _path: str) -> None:
        self._disk_timer.start()

    def check_disk(self) -> str:
        """파일이 다른 곳에서 바뀌었는지 보고 알맞게 대응. 반환은 disk_change 결과."""
        self._watch()
        change = M.disk_change(self.path, self._disk)
        if change == "same":
            return change
        if change == "other":
            # 시간표 밖만 바뀜: 저장해도 그 변경은 남으니 기준만 갱신.
            self._disk = M.DiskState.read(self.path)
            self.statusBar().showMessage(
                f"{self.path.name} 의 다른 설정이 바뀌었습니다. 시간표에는 영향 없습니다.", 6000)
        elif change == "timetable" and self.undo.isClean():
            # 편집 중인 것이 없으면 조용히 따라갑니다.
            self._load_into_editor()
            self.statusBar().showMessage(
                f"다른 곳에서 {self.path.name} 의 시간표가 바뀌어 다시 읽었습니다.", 6000)
        elif change == "timetable":
            self.banner_label.setText(
                f"⚠ 이 편집기를 연 뒤 다른 곳에서 {self.path.name} 의 시간표·공휴일이 "
                "바뀌었습니다. 지금 저장하면 그 변경을 덮어씁니다.")
            self.banner.show()
        elif change == "broken":
            self.banner_label.setText(
                f"⚠ {self.path.name} 이 지금 올바른 TOML 이 아닙니다(다른 곳에서 고치는 중일 수 "
                "있음). 고쳐질 때까지 저장하지 마세요.")
            self.banner.show()
        return change

    def _conflict_ok(self) -> bool:
        """저장 직전 확인. True 면 저장해도 됨(덮어쓰기에 동의했거나 충돌 없음)."""
        change = M.disk_change(self.path, self._disk)
        if change in ("same", "other"):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("다른 곳에서 바뀐 파일")
        if change == "broken":
            box.setText(f"{self.path.name} 이 지금 올바른 TOML 이 아니라 저장할 수 없습니다.\n"
                        "다른 곳에서 고치는 중이면 끝난 뒤 다시 저장하세요.")
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            return False
        box.setText(f"이 편집기를 연 뒤 다른 곳에서 {self.path.name} 의 시간표·공휴일이 "
                    "바뀌었습니다.")
        box.setInformativeText("덮어쓰면 그쪽 변경은 사라집니다(원본은 .bak 으로 남음).")
        over = box.addButton("내 것으로 덮어쓰기", QMessageBox.ButtonRole.DestructiveRole)
        reload = box.addButton("디스크 내용 다시 읽기", QMessageBox.ButtonRole.ResetRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is over:
            return True
        if box.clickedButton() is reload:
            self._load_into_editor()
        return False

    def _reload_discarding(self) -> None:
        self._load_into_editor()
        self.statusBar().showMessage(f"{self.path.name} 을 다시 읽었습니다.", 6000)

    # -- 화면 구성 ------------------------------------------------------
    def _build_grid_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        hint = QLabel("빈 칸 드래그 = 추가 · 블록 끌기 = 이동(Shift: 그 요일만) · "
                      "아래 가장자리 = 길이 · 더블클릭 = 수정 · 우클릭 = 메뉴")
        hint.setStyleSheet("color: #6B7280;")
        lay.addWidget(hint)
        lay.addWidget(self.view, 1)
        return w

    def _build_list_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["수업명", "요일", "시간", "강의실", "교수", "전사 언어", "과목코드"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.edit_course(r))
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        for text, fn in (("추가", lambda: self.add_course()),
                         ("수정", lambda: self._selected() is not None
                          and self.edit_course(self._selected())),
                         ("복제", lambda: self._selected() is not None
                          and self.duplicate_course(self._selected())),
                         ("삭제", lambda: self._selected() is not None
                          and self.delete_course(self._selected()))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _build_holiday_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        top.addWidget(QLabel("여기 적은 날짜의 수업은 녹화·송출 일정에서 건너뜁니다."))
        top.addStretch(1)
        self.summary_btn = QPushButton("다가오는 일정 · 공휴일 보기")
        self.summary_btn.setToolTip("앞으로 녹화될 수업과 공휴일로 쉬는 회차를 확인합니다 (Ctrl+I)")
        self.summary_btn.clicked.connect(self.show_summary)
        top.addWidget(self.summary_btn)
        lay.addLayout(top)
        self.htable = QTableWidget(0, 2)
        self.htable.setHorizontalHeaderLabels(["날짜", "이름"])
        self.htable.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.htable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.htable.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.htable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.htable.verticalHeader().setVisible(False)
        lay.addWidget(self.htable, 1)
        row = QHBoxLayout()
        self.htable.cellDoubleClicked.connect(
            lambda r, _c: self.edit_holiday(self._holiday_at(self.htable, r)))
        for text, fn in (("추가", self.add_holiday), ("붙여넣기로 추가", self.paste_holidays),
                         ("수정", lambda: self.edit_holiday(self._selected_holiday(self.htable))),
                         ("삭제", self.delete_holidays), ("지난 날짜 지우기", self.drop_past_holidays)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _build_menus(self) -> None:
        def act(menu, text, fn, shortcut=None):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(shortcut)
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        f = self.menuBar().addMenu("파일(&F)")
        act(f, "config.toml 열기…", self.open_other, QKeySequence.StandardKey.Open)
        act(f, "디스크에서 다시 읽기", self.reload_from_disk)
        act(f, "저장", self.save, QKeySequence.StandardKey.Save)
        act(f, "다가오는 일정 · 공휴일 보기…", self.show_summary, "Ctrl+I")
        f.addSeparator()
        act(f, "Swift 앱 백업 JSON 가져오기…", self.import_swift)
        act(f, "Swift 앱 백업 JSON 내보내기…", self.export_swift)
        f.addSeparator()
        act(f, "끝내기", self.close, QKeySequence.StandardKey.Quit)

        e = self.menuBar().addMenu("편집(&E)")
        u = self.undo.createUndoAction(self, "실행 취소")
        u.setShortcut(QKeySequence.StandardKey.Undo)
        r = self.undo.createRedoAction(self, "다시 실행")
        r.setShortcut(QKeySequence.StandardKey.Redo)
        e.addAction(u)
        e.addAction(r)
        e.addSeparator()
        act(e, "수업 추가…", lambda: self.add_course(), QKeySequence.StandardKey.New)
        act(e, "선택한 수업 삭제", self._delete_selected, QKeySequence.StandardKey.Delete)

        v = self.menuBar().addMenu("보기(&V)")
        wk = act(v, "주말(토·일) 표시", self._toggle_weekend)
        wk.setCheckable(True)

    # -- 모델 <-> 화면 ---------------------------------------------------
    def _set_model(self, tt: M.Timetable) -> None:
        self.tt = tt
        self._refresh()

    def _apply(self, new: M.Timetable, text: str) -> None:
        self.undo.push(_Snapshot(self, self.tt.copy(), new, text))

    def _refresh(self) -> None:
        self.scene.rebuild(self.tt, self.show_weekend)
        self.table.setRowCount(len(self.tt.courses))
        for r, c in enumerate(self.tt.courses):
            vals = [c.name, c.days_text, c.time_text, c.room, c.professor,
                    c.language or "기본", c.course_id]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if col == 0:
                    sw = QColor(c.color)
                    item.setBackground(QBrush(sw))
                    item.setForeground(QBrush(_text_color(sw)))
                self.table.setItem(r, col, item)
        hs = sorted(self.tt.holidays, key=lambda h: h.day)
        self.htable.setRowCount(len(hs))
        for r, h in enumerate(hs):
            dt = QTableWidgetItem(f"{h.day.isoformat()} ({M.DAY_KR[h.day.weekday()]})")
            dt.setData(Qt.ItemDataRole.UserRole, h.day.isoformat())
            if h.day < date.today():
                dt.setForeground(QBrush(QColor("#9CA3AF")))
            self.htable.setItem(r, 0, dt)
            self.htable.setItem(r, 1, QTableWidgetItem(h.name))
        self._update_status()

    def _update_status(self) -> None:
        ov = self.tt.overlaps()
        msg = f"수업 {len(self.tt.courses)}개 · 공휴일 {len(self.tt.holidays)}일 · {self.path}"
        if ov:
            pairs = "; ".join(f"{M.DAY_KR[M.DAY_INDEX[d]]} {a.name}↔{b.name}" for d, a, b in ov[:3])
            msg = f"⚠ 시간 겹침 {len(ov)}건 ({pairs}) · " + msg
        self.statusBar().showMessage(msg)
        self._update_title()

    def _update_title(self) -> None:
        star = "" if self.undo.isClean() else " *"
        self.setWindowTitle(f"Sync Note 시간표 편집기 — {self.path.name}{star}")

    def _selected(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    # -- 수업 ------------------------------------------------------------
    def add_course(self, prefill: M.Course | None = None) -> None:
        used = {c.color.upper() for c in self.tt.courses}
        color = next((p for p in M.PALETTE if p not in used), M.PALETTE[len(used) % len(M.PALETTE)])
        base = prefill or M.Course(name="", days=["mon"], color=color)
        if prefill and prefill.color == M.DEFAULT_COLOR:
            base = prefill.copy(color=color)
        got = CourseEditor(self, base, "수업 추가").exec()
        if got:
            new = self.tt.copy()
            new.courses.append(got)
            self._apply(new, f"{got.name} 추가")

    def edit_course(self, idx: int) -> None:
        if not (0 <= idx < len(self.tt.courses)):
            return
        got = CourseEditor(self, self.tt.courses[idx], "수업 수정").exec()
        if got:
            new = self.tt.copy()
            new.courses[idx] = got
            self._apply(new, f"{got.name} 수정")

    def duplicate_course(self, idx: int) -> None:
        if 0 <= idx < len(self.tt.courses):
            src = self.tt.courses[idx]
            self.add_course(src.copy(name=src.name + " (복사)", days=[]))

    def delete_course(self, idx: int) -> None:
        if not (0 <= idx < len(self.tt.courses)):
            return
        c = self.tt.courses[idx]
        if QMessageBox.question(self, "삭제", f"'{c.name}' ({c.days_text} {c.time_text}) 를 지울까요?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        new = self.tt.copy()
        del new.courses[idx]
        self._apply(new, f"{c.name} 삭제")

    def _delete_selected(self) -> None:
        if self.tabs.currentIndex() == 2:
            self.delete_holidays()
            return
        blocks = [i for i in self.scene.selectedItems() if isinstance(i, CourseBlock)]
        if self.tabs.currentIndex() == 0 and blocks:
            self._remove_day(blocks[0].idx, blocks[0].day)
            return
        if (idx := self._selected()) is not None:
            self.delete_course(idx)

    def _remove_day(self, idx: int, day: str) -> None:
        c = self.tt.courses[idx]
        if len(c.days) <= 1:
            self.delete_course(idx)
            return
        new = self.tt.copy()
        new.courses[idx].days = [d for d in c.days if d != day]
        self._apply(new, f"{c.name} {M.DAY_KR[M.DAY_INDEX[day]]} 빼기")

    def _on_create(self, day: str, start: int, end: int) -> None:
        self.add_course(M.Course(name="", days=[day], start=M.from_minutes(start),
                                 end=M.from_minutes(end)))

    def _on_move(self, idx: int, from_day: str, to_day: str, start: int, end: int,
                 only_this: bool) -> None:
        if not (0 <= idx < len(self.tt.courses)):
            self._refresh()
            return
        c = self.tt.courses[idx]
        s, e = M.from_minutes(start), M.from_minutes(end)
        if (s, e, to_day) == (c.start, c.end, from_day):
            self._refresh()                 # 제자리 — 블록 위치만 되돌림
            return
        new = self.tt.copy()
        if only_this and len(c.days) > 1:
            new.courses[idx].days = [d for d in c.days if d != from_day]
            new.courses.append(c.copy(days=[to_day], start=s, end=e))
            self._apply(new, f"{c.name} {M.DAY_KR[M.DAY_INDEX[from_day]]}만 이동")
            return
        if to_day != from_day and to_day in c.days:
            self.statusBar().showMessage(
                f"'{c.name}' 은 이미 {M.DAY_KR[M.DAY_INDEX[to_day]]}요일에 있습니다.", 5000)
            self._refresh()
            return
        days = [to_day if d == from_day else d for d in c.days]
        new.courses[idx] = c.copy(days=days, start=s, end=e)
        self._apply(new, f"{c.name} 이동")

    def _on_context(self, idx: int, day: str, screen_pos) -> None:
        if not (0 <= idx < len(self.tt.courses)):
            return
        c = self.tt.courses[idx]
        m = QMenu(self)
        m.addAction("수정…", lambda: self.edit_course(idx))
        m.addAction("복제…", lambda: self.duplicate_course(idx))
        if len(c.days) > 1:
            m.addAction(f"{M.DAY_KR[M.DAY_INDEX[day]]}요일만 빼기",
                        lambda: self._remove_day(idx, day))
        m.addAction("수업 삭제", lambda: self.delete_course(idx))
        m.exec(screen_pos)

    def _toggle_weekend(self, on: bool) -> None:
        self.show_weekend = bool(on)
        self._refresh()

    # -- 공휴일 ----------------------------------------------------------
    def add_holiday(self) -> None:
        h = ask_holiday(self)
        if h:
            self._merge_holidays([h], "공휴일 추가")

    def paste_holidays(self) -> None:
        hs = ask_holiday_paste(self)
        if hs:
            self._merge_holidays(hs, f"공휴일 {len(hs)}일 추가")

    def _merge_holidays(self, hs: list[M.Holiday], text: str) -> None:
        new = self.tt.copy()
        have = {h.day: h for h in new.holidays}
        for h in hs:
            if h.day in have:
                if h.name and not have[h.day].name:
                    have[h.day].name = h.name
            else:
                new.holidays.append(h)
                have[h.day] = h
        self._apply(new, text)

    @staticmethod
    def _holiday_at(table: QTableWidget, row: int) -> date | None:
        item = table.item(row, 0) if row >= 0 else None
        raw = item.data(Qt.ItemDataRole.UserRole) if item else None
        return date.fromisoformat(raw) if raw else None

    def _selected_holiday(self, table: QTableWidget) -> date | None:
        rows = table.selectionModel().selectedRows() if table.selectionModel() else []
        return self._holiday_at(table, rows[0].row()) if rows else None

    def edit_holiday(self, day: date | None) -> None:
        cur = next((h for h in self.tt.holidays if h.day == day), None)
        if cur is None:
            return
        got = ask_holiday(self, cur)
        if got is None or (got.day, got.name) == (cur.day, cur.name):
            return
        try:
            new = M.edit_holiday(self.tt, cur.day, got)
        except M.TimetableError as exc:
            QMessageBox.warning(self, "공휴일 수정", str(exc))
            return
        self._apply(new, f"공휴일 {cur.day:%m-%d} 수정")

    def delete_holiday_days(self, days) -> None:
        days = [d for d in days if d is not None]
        if days:
            self._apply(M.remove_holidays(self.tt, days), f"공휴일 {len(days)}일 삭제")

    def delete_holidays(self) -> None:
        self.delete_holiday_days(
            {self._holiday_at(self.htable, i.row())
             for i in self.htable.selectionModel().selectedRows()})

    def drop_past_holidays(self) -> None:
        today = date.today()
        if any(h.day < today for h in self.tt.holidays):
            new = self.tt.copy()
            new.holidays = [h for h in new.holidays if h.day >= today]
            self._apply(new, "지난 공휴일 지우기")

    # -- 파일 ------------------------------------------------------------
    def _confirm_discard(self) -> bool:
        if self.undo.isClean():
            return True
        ans = QMessageBox.question(
            self, "저장하지 않은 변경",
            "저장하지 않은 변경이 있습니다. 저장할까요?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if ans == QMessageBox.StandardButton.Save:
            return self.save(show_summary=False)   # 닫는 중엔 결과 창을 안 띄움
        return ans == QMessageBox.StandardButton.Discard

    def reload_from_disk(self) -> None:
        if not self._confirm_discard():
            return
        self._load_into_editor(report_errors=True)

    def _load_into_editor(self, report_errors: bool = False) -> None:
        """확인 없이 파일을 읽어 편집기를 그것으로 바꿉니다(실행 취소 기록도 비움)."""
        try:
            tt = M.load(self.path)
        except M.TimetableError as exc:
            if report_errors:
                QMessageBox.critical(self, "읽기 실패", str(exc))
            else:
                self.statusBar().showMessage(f"다시 읽지 못했습니다: {exc}", 8000)
                return
            tt = M.Timetable()
        self.undo.clear()
        self._set_model(tt)
        self._remember_disk()
        if not self.path.exists():
            self.statusBar().showMessage(
                f"{self.path} 가 없습니다. 저장하면 새로 만듭니다(나머지 설정은 기본값).")

    def open_other(self) -> None:
        if not self._confirm_discard():
            return
        name, _ = QFileDialog.getOpenFileName(self, "config.toml 열기", str(self.path.parent),
                                              "TOML (*.toml);;모든 파일 (*)")
        if name:
            self.path = Path(name)
            self.undo.setClean()    # 위에서 이미 확인함
            self.reload_from_disk()

    def save(self, show_summary: bool = True) -> bool:
        if not self._conflict_ok():
            return False
        try:
            commented = M.save(self.path, self.tt)
        except (M.TimetableError, OSError) as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))
            return False
        self.undo.setClean()
        self._remember_disk()        # 내가 쓴 것을 '다른 곳의 변경' 으로 오인하지 않게
        note = f"저장했습니다: {self.path} (원본은 {self.path.name}.bak)"
        if commented:
            QMessageBox.information(
                self, "손으로 적은 시간표",
                f"config.toml 에 손으로 적은 시간표/공휴일 표 {commented}개를 주석 처리했습니다.\n"
                "같은 수업이 두 번 잡히지 않게 하기 위해서이며, 지우지는 않았습니다.")
        self.statusBar().showMessage(note, 6000)
        if show_summary:
            self.show_summary()
        return True

    def show_summary(self) -> SaveSummary:
        """저장 결과 창. 모달이 아니라 편집을 막지 않고, 하나만 띄웁니다."""
        if self.summary is None:
            self.summary = SaveSummary(self)
        self.summary.refresh()
        self.summary.show()
        self.summary.raise_()
        self.summary.activateWindow()
        return self.summary

    def import_swift(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Swift 앱 백업 JSON", "",
                                              "JSON (*.json *.txt);;모든 파일 (*)")
        if not name:
            return
        try:
            got = M.from_swift_json(Path(name).read_text(encoding="utf-8-sig"))
        except (M.TimetableError, OSError, UnicodeDecodeError) as exc:
            QMessageBox.critical(self, "가져오기 실패", str(exc))
            return
        box = QMessageBox(self)
        box.setWindowTitle("가져오기")
        box.setText(f"수업 {len(got.courses)}개, 공휴일 {len(got.holidays)}일을 읽었습니다.")
        merge = box.addButton("지금 시간표에 합치기", QMessageBox.ButtonRole.AcceptRole)
        repl = box.addButton("지금 시간표를 바꾸기", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is merge:
            new = self.tt.copy()
            new.courses.extend(got.courses)
            self._apply(new, "Swift JSON 합치기")
            self._merge_holidays(got.holidays, "Swift JSON 공휴일")
        elif box.clickedButton() is repl:
            self._apply(got, "Swift JSON 으로 바꾸기")

    def export_swift(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Swift 앱 백업 JSON 내보내기",
                                              "timetable-backup.json", "JSON (*.json)")
        if not name:
            return
        text, warnings = M.to_swift_json(self.tt)
        Path(name).write_text(text, encoding="utf-8")
        msg = f"내보냈습니다: {name}\nSwift 앱 설정 → '데이터 복원' 에 붙여넣으세요."
        if warnings:
            msg += "\n\n" + "\n".join(warnings)
        QMessageBox.information(self, "내보내기", msg)

    def changeEvent(self, event) -> None:
        # Windows 설정에서 라이트/다크를 바꾸면 팔레트가 바뀝니다. 격자는
        # 그릴 때 색을 정하므로 다시 그려야 따라갑니다.
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._refresh()
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def _install_korean(app: QApplication) -> None:
    """확인/취소 같은 Qt 기본 버튼을 한국어로. 번역 파일이 없으면 그냥 영어."""
    tr = QTranslator(app)
    if tr.load(QLocale(QLocale.Language.Korean), "qtbase", "_",
               QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(tr)
        app._syncnote_tr = tr   # 수명 유지


def run(config_path: Path, argv: list[str] | None = None) -> int:
    app = QApplication.instance() or QApplication(argv or sys.argv[:1])
    _install_korean(app)
    win = TimetableWindow(config_path)
    win.show()
    # 첫 화면은 아침 수업 근처가 보이게.
    QTimer.singleShot(0, lambda: win.view.ensureVisible(
        QRectF(0, win.scene.y_of(8 * 60), 10, 10), 0, 40))
    return app.exec()
