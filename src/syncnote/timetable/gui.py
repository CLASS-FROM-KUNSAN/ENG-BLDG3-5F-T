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
from collections.abc import Callable
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
    QObject,
    QPoint,
    QRect,
    QRectF,
    QSize,
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
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QScreen,
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
    QCompleter,
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
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import settings_schema
from ..eclass import catalog as eclass_catalog
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


# ---------------------------------------------------------------------------
# 창을 '작업 가능한 영역' 안에 두기
# ---------------------------------------------------------------------------
# 기기마다 화면 크기·배율·작업 표시줄 위치가 달라, 고정 크기(예: 높이 900)
# 로 열면 작은 노트북에서 창 아래가 작업 표시줄 뒤로 넘어갑니다. 크기를
# 고정하지 않고, 사용 가능한 영역(availableGeometry = 화면 - 작업 표시줄)에
# 맞춰 줄이고 그 안으로 되돌립니다.
FIT_MARGIN = 8          # 영역 가장자리와 띄울 여백(px)
FIT_START_RATIO = 0.92  # 처음 열 때 영역의 이 비율까지만 차지


def fit_rect(frame: QRect, avail: QRect, margin: int = FIT_MARGIN) -> QRect:
    """frame(창 테두리 포함)을 avail 안에 들어가게 한 사각형.

    너무 크면 먼저 줄이고, 그다음 밖으로 나간 만큼만 안으로 밉니다. 이미
    안에 있으면 그대로 돌려줍니다(사용자가 둔 자리를 존중).
    """
    room = avail.adjusted(margin, margin, -margin, -margin)
    if room.width() <= 0 or room.height() <= 0:
        room = QRect(avail)
    w = min(frame.width(), room.width())
    h = min(frame.height(), room.height())
    x = min(max(frame.x(), room.left()), room.right() - w + 1)
    y = min(max(frame.y(), room.top()), room.bottom() - h + 1)
    return QRect(x, y, w, h)


def start_rect(want: QSize, avail: QRect, chrome: QSize = QSize(0, 0)) -> QRect:
    """처음 열 때의 창 테두리 사각형: 원하는 크기를 영역 비율로 줄여 가운데."""
    w = min(want.width() + chrome.width(), int(avail.width() * FIT_START_RATIO))
    h = min(want.height() + chrome.height(), int(avail.height() * FIT_START_RATIO))
    return QRect(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2, w, h)


class ScreenKeeper(QObject):
    """최상위 창 하나를 사용 가능한 영역 안에 붙잡아 둡니다.

    - 처음 보일 때: 원하는 크기를 영역에 맞춰 줄이고 가운데로(want 를 준 경우).
    - 옮기거나 크기를 바꾼 뒤: 마우스를 놓고 잠깐 뒤 영역 밖이면 되돌립니다.
      끄는 도중에는 건드리지 않습니다(사용자와 싸우지 않게).
    - 다른 모니터로 옮기거나 해상도·작업 표시줄이 바뀌어도 다시 맞춥니다.
    - 최대화·전체 화면·최소화 상태는 OS 가 관리하므로 손대지 않습니다.
    """

    DELAY_MS = 250

    def __init__(self, win: QWidget, want: QSize | Callable[[], QSize] | None = None) -> None:
        super().__init__(win)            # Qt 부모 = 창. 창 참조는 parent() 로만 얻음
        self.want = want
        self._placed = False
        self._screen: QScreen | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.DELAY_MS)
        self.timer.timeout.connect(self.check)
        # 창이 이 객체를 붙잡게 합니다(안 그러면 PySide 가 파이썬 쪽을 정리해
        # eventFilter 가 속성 없는 객체로 불림 — 실제로 났음). 반대로 이 객체는
        # 창을 속성으로 들지 않습니다: 서로 붙잡으면 순환 참조가 되고, GC 가 그것을
        # 아무 때나 치우면서 Qt 의 부모·자식 정리와 엇갈려 접근 위반으로 죽었습니다.
        win._screen_keeper = self
        win.installEventFilter(self)

    # 창마다 하나. 이미 있으면 그것을 돌려줍니다.
    @classmethod
    def attach(cls, win: QWidget,
               want: QSize | Callable[[], QSize] | None = None) -> ScreenKeeper:
        k = getattr(win, "_screen_keeper", None)
        return k if isinstance(k, ScreenKeeper) else cls(win, want)

    @property
    def win(self) -> QWidget | None:
        return self.parent()

    def eventFilter(self, obj, event) -> bool:
        if "want" not in self.__dict__:   # 정리 중인 객체 — 아무것도 안 함
            return False
        win = self.win
        if win is None:
            return False
        t = event.type()
        if obj is win:
            if t == QEvent.Type.Show:
                self._hook_screen()
                if not self._placed:
                    self._placed = True
                    self.place_initial()
                else:
                    self.check()
            elif t in (QEvent.Type.Move, QEvent.Type.Resize):
                self.timer.start()
        return False

    def _avail(self) -> QRect:
        fr = self.win.frameGeometry()
        screen = QGuiApplication.screenAt(fr.center()) or self.win.screen() \
            or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _hook_screen(self) -> None:
        handle = self.win.windowHandle()
        if handle is not None:
            try:
                handle.screenChanged.connect(self._on_screen, Qt.ConnectionType.UniqueConnection)
            except (RuntimeError, TypeError):
                pass
            self._on_screen(handle.screen())

    def _on_screen(self, screen: QScreen | None) -> None:
        if screen is self._screen:
            return
        if self._screen is not None:
            try:
                self._screen.availableGeometryChanged.disconnect(self._on_avail)
            except (RuntimeError, TypeError):
                pass
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_avail)
        self.timer.start()

    def _on_avail(self, _rect: QRect) -> None:
        self.timer.start()

    def _chrome(self) -> tuple[int, int, int, int]:
        """창 테두리·제목줄 두께 (왼, 위, 오른, 아래)."""
        fr, g = self.win.frameGeometry(), self.win.geometry()
        return (g.left() - fr.left(), g.top() - fr.top(),
                fr.right() - g.right(), fr.bottom() - g.bottom())

    def _apply(self, frame: QRect) -> None:
        left, top, right, bottom = self._chrome()
        self.win.resize(max(1, frame.width() - left - right),
                        max(1, frame.height() - top - bottom))
        # 최소 크기 때문에 덜 줄었을 수 있습니다. 실제 크기로 자리를 다시 잡되,
        # 그래도 영역보다 크면 제목줄·왼쪽 위가 보이도록 왼쪽 위에 붙입니다
        # (제목줄이 화면 밖이면 사용자가 창을 다시 끌어올 수 없습니다).
        real = QRect(frame.topLeft(), self.win.frameGeometry().size())
        self.win.move(self._position(real))   # 최상위 창의 move 는 테두리 기준

    def _position(self, frame: QRect) -> QPoint:
        room = self._avail().adjusted(FIT_MARGIN, FIT_MARGIN, -FIT_MARGIN, -FIT_MARGIN)
        x = max(room.left(), min(frame.x(), room.right() - frame.width() + 1))
        y = max(room.top(), min(frame.y(), room.bottom() - frame.height() + 1))
        return QPoint(x, y)

    def place_initial(self) -> None:
        if self.want is None:
            self.check()
            return
        if self._skip():
            return
        # want 가 함수면 지금(레이아웃이 잡힌 뒤) 계산합니다 — 글자 실제 폭을 재려고.
        want = self.want() if callable(self.want) else self.want
        left, top, right, bottom = self._chrome()
        self._apply(start_rect(want, self._avail(), QSize(left + right, top + bottom)))

    def _skip(self) -> bool:
        w = self.win
        if w is None:
            return True
        return (not w.isVisible() or w.isMaximized() or w.isFullScreen() or w.isMinimized())

    def check(self) -> bool:
        """영역 밖이면 되돌림. 되돌렸으면 True."""
        if self._skip():
            return False
        if QGuiApplication.mouseButtons() != Qt.MouseButton.NoButton:
            self.timer.start()          # 아직 끄는 중 — 놓을 때까지 기다림
            return False
        fr = self.win.frameGeometry()
        new = fit_rect(fr, self._avail())
        if new == fr:
            return False
        # 더 줄일 수 없는 창(최소 크기)이 이미 '가능한 최선' 자리에 있으면
        # 아무것도 안 합니다. 안 그러면 같은 이동을 끝없이 되풀이합니다.
        mins = self.win.minimumSizeHint().expandedTo(self.win.minimumSize())
        left, top, right, bottom = self._chrome()
        cant_shrink = (new.width() - left - right < mins.width()
                       or new.height() - top - bottom < mins.height())
        if cant_shrink and self._position(fr) == fr.topLeft() \
                and fr.width() - left - right <= max(mins.width(), new.width())  \
                and fr.height() - top - bottom <= max(mins.height(), new.height()):
            return False
        before = QRect(fr)
        self._apply(new)
        return self.win.frameGeometry() != before


def natural_width(label: QLabel) -> int:
    """줄바꿈 없이 한 줄로 그릴 때의 폭.

    글꼴 지표(fontMetrics)로 직접 재면 안 됩니다. 한글이 기본 글꼴에 없으면
    대체 글꼴로 그려 실제 폭이 더 넓은데, 지표는 기본 글꼴 기준이라 좁게
    나와 결국 두 줄로 접혔습니다. QLabel 이 실제로 쓰는 배치로 잽니다.
    """
    wrap = label.wordWrap()
    if wrap:
        label.setWordWrap(False)
    try:
        return label.sizeHint().width()
    finally:
        if wrap:
            label.setWordWrap(True)

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


class _OnResize(QObject):
    """위젯 크기가 바뀔 때마다 콜백(줄바꿈 글자의 높이 맞추기용)."""

    def __init__(self, widget: QWidget, callback: Callable[[], None]) -> None:
        super().__init__(widget)
        self._cb = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._cb()
        return False


class CourseEditor:
    """course_dialog.ui 를 감싸 Course 를 주고받습니다."""

    def __init__(self, parent: QWidget | None, course: M.Course, title: str,
                 catalog: list[eclass_catalog.Entry] | None = None) -> None:
        self.catalog = list(catalog or [])
        self.dlg: QDialog = load_ui("course_dialog.ui", parent)
        # 처음 폭은 eClass 제안 문구가 한 줄로 들어가게(자리가 있을 때만).
        ScreenKeeper.attach(self.dlg, self._preferred_size)
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
        self.suggestion: QLabel = w(QLabel, "eclassSuggestion")
        self.apply_btn: QPushButton = w(QPushButton, "eclassApply")
        # 버튼은 글자 폭만, 남는 폭은 제안 문구가 씁니다.
        self.apply_btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row = self.dlg.findChild(QHBoxLayout, "eclassRow")
        if row is not None:
            row.setStretch(0, 1)
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

        # eClass 과목 연결: 이름 자동 완성 + 가장 가까운 과목 제안.
        self._sugg: eclass_catalog.Suggestion | None = None
        if self.catalog:
            comp = QCompleter([e.name for e in self.catalog], self.dlg)
            comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            comp.setFilterMode(Qt.MatchFlag.MatchContains)
            comp.activated.connect(lambda _t: self._suggest())
            self.name.setCompleter(comp)
        self.name.textChanged.connect(self._suggest)
        self.course_id.textChanged.connect(self._suggest)
        self.apply_btn.clicked.connect(self.apply_suggestion)
        self._wrap_filter = _OnResize(self.suggestion, self._fit_suggestion_height)
        self._suggest()
        self._refresh()

    def _preferred_size(self) -> QSize:
        hint = self.dlg.sizeHint()
        extra = natural_width(self.suggestion) - self.suggestion.width()
        return QSize(max(hint.width(), self.dlg.width() + max(0, extra)), hint.height())

    def _fit_suggestion_height(self) -> None:
        # 접혔을 때만 줄 높이가 늘도록(폼 안의 줄바꿈 글자는 스스로 안 늘어남).
        # 실제 폭을 아직 모르면(창이 뜨기 전) 높이를 묶지 않습니다 — 폭이
        # 정해지면 WrapHeight 필터가 다시 부릅니다.
        w = self.suggestion.width()
        h = self.suggestion.heightForWidth(w) if self.suggestion.isVisible() and w > 0 else 0
        self.suggestion.setMinimumHeight(max(0, h))

    # -- eClass 과목 ----------------------------------------------------
    def _suggest(self, *_a) -> None:
        try:
            self._suggest_text()
        finally:
            self._fit_suggestion_height()

    def _suggest_text(self) -> None:
        if not self.catalog:
            self._sugg = None
            self.suggestion.setText("과목 목록이 아직 없습니다. eClass 수집을 한 번 돌리면 "
                                    "과목코드를 고를 수 있습니다.")
            self.suggestion.setStyleSheet("color: #6B7280;")
            self.apply_btn.setEnabled(False)
            return
        s = eclass_catalog.suggest(self.name.text(), self.catalog, self.course_id.text())
        self._sugg = s
        cid = self.course_id.text().strip()
        if s.entry is None:
            self.suggestion.setText("비슷한 eClass 과목이 없습니다." if self.name.text().strip()
                                    else "수업명을 적으면 eClass 과목을 찾아 드립니다.")
            self.suggestion.setStyleSheet("color: #6B7280;")
            self.apply_btn.setEnabled(False)
            return
        linked = bool(cid) and cid == s.entry.id and self.name.text().strip() == s.entry.name
        if linked:
            self.suggestion.setText(f"✔ 연결됨: {s.entry.name} [{s.entry.id}]")
            self.suggestion.setToolTip(s.entry.label)
            self.suggestion.setStyleSheet("color: #15803D;")
            self.apply_btn.setEnabled(False)
            return
        warn = " · ⚠ 비슷한 과목이 여럿" if s.ambiguous else ""
        no_id = " · 코드 모름" if not s.entry.id else ""
        code = f" [{s.entry.id}]" if s.entry.id else ""
        self.suggestion.setText(f"{s.entry.name}{code} · 일치 {s.score:.0%}{warn}{no_id}")
        self.suggestion.setToolTip(s.entry.label)
        self.suggestion.setStyleSheet("")
        self.apply_btn.setEnabled(True)

    def apply_suggestion(self) -> None:
        """제안된 과목의 정식 이름과 코드로 채웁니다(코드를 모르면 이름만)."""
        s = self._sugg
        if s is None or s.entry is None:
            return
        self.name.setText(s.entry.name)
        if s.entry.id:
            self.course_id.setText(s.entry.id)
        self._suggest()
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
    ScreenKeeper.attach(dlg)
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
    ScreenKeeper.attach(dlg)
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
        # 처음 폭은 가장 긴 줄(예: '… · 쉼(개교기념일)')이 잘리지 않게. 영역보다
        # 넓으면 ScreenKeeper 가 줄이고, 그때만 가로 스크롤이 생깁니다.
        ScreenKeeper.attach(self, self._preferred_size)
        lay = QVBoxLayout(self)

        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)

        lay.addWidget(QLabel(f"<b>다음 녹화</b> (앞으로 {self.COUNT}회)"))
        self.next_list = QListWidget()
        lay.addWidget(self.next_list, 3)

        hl = QLabel("<b>다가오는 공휴일</b> — 이 날의 수업은 녹화·송출하지 않습니다")
        hl.setWordWrap(True)
        lay.addWidget(hl)
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

    def _preferred_size(self) -> QSize:
        longest = self.next_list.sizeHintForColumn(0) if self.next_list.count() else 0
        frame = 2 * self.next_list.frameWidth() + self.next_list.verticalScrollBar().sizeHint().width()
        pad = max(0, self.width() - self.next_list.width())
        return QSize(max(520, longest + frame + pad + 8), 560)

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


class LinkDialog(QDialog):
    """과목코드가 빈 수업을 eClass 과목과 한꺼번에 연결.

    확신 있는 제안(임계값 이상·1·2위가 붙지 않음·코드 있음)만 미리 체크합니다.
    적용하면 수업명을 eClass 정식 이름으로, 과목코드를 채웁니다(한 번에 실행 취소).
    """

    def __init__(self, parent: QWidget, tt: M.Timetable,
                 catalog: list[eclass_catalog.Entry]) -> None:
        super().__init__(parent)
        self.setWindowTitle("eClass 과목과 연결")
        self.resize(640, 420)
        ScreenKeeper.attach(self)
        self.tt = tt
        self.applied = 0
        lay = QVBoxLayout(self)
        info = QLabel("과목코드가 비어 있는 수업입니다. 체크한 줄은 eClass 과목명과 과목코드로 "
                      "바뀝니다. 과목코드가 있으면 녹화 파일이 어느 과목인지 정확히 판정됩니다.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.rows: list[tuple[int, QComboBox]] = []
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["연결", "시간표 수업", "eClass 과목"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        coded = [e for e in catalog if e.id]
        # 콤보 데이터에는 번호만 넣습니다. 파이썬 객체를 QVariant 로 넣으면 다른 창이
        # 정리되는 순간과 겹칠 때 접근 위반으로 죽은 적이 있습니다.
        self.coded = coded
        for idx, c in enumerate(tt.courses):
            if c.course_id.strip():
                continue
            s = eclass_catalog.suggest(c.name, coded)
            r = self.table.rowCount()
            self.table.insertRow(r)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Checked if s.confident
                                else Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, check)
            self.table.setItem(r, 1, QTableWidgetItem(f"{c.name}  ({c.days_text})"))
            combo = QComboBox()
            combo.addItem("— 연결 안 함 —", -1)
            for n, e in enumerate(coded):
                combo.addItem(e.label, n)
            if s.entry is not None:
                combo.setCurrentIndex(coded.index(s.entry) + 1)
                combo.setToolTip(f"일치 {s.score:.0%}" + (" · 비슷한 과목이 여럿" if s.ambiguous else ""))
            combo.currentIndexChanged.connect(
                lambda i, item=check: item.setCheckState(
                    Qt.CheckState.Checked if i > 0 else Qt.CheckState.Unchecked))
            self.table.setCellWidget(r, 2, combo)
            self.rows.append((idx, combo))
        if not self.rows:
            lay.addWidget(QLabel("모든 수업에 이미 과목코드가 있습니다."))
        lay.addWidget(self.table, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def result_timetable(self) -> M.Timetable | None:
        new = self.tt.copy()
        self.applied = 0
        for r, (idx, combo) in enumerate(self.rows):
            n = combo.currentData()
            entry = self.coded[n] if isinstance(n, int) and n >= 0 else None
            if self.table.item(r, 0).checkState() != Qt.CheckState.Checked or entry is None:
                continue
            new.courses[idx] = new.courses[idx].copy(name=entry.name, course_id=entry.id)
            self.applied += 1
        return new if self.applied else None


# ---------------------------------------------------------------------------
# 메인 창
# ---------------------------------------------------------------------------
class TimetableWindow(QMainWindow):
    # 저장 뒤 설치·점검 흐름(setup.bat → 라이브러리 → 비밀번호 → doctor/devices).
    # 테스트는 conftest 에서 끕니다(진짜 pip install 을 돌리면 안 되므로).
    SETUP_AFTER_SAVE = True

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.path = Path(config_path)
        self.tt = M.Timetable()
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(lambda _c: self._update_title())
        self.show_weekend = False
        self.summary: SaveSummary | None = None
        self.catalog: list[eclass_catalog.Entry] = []

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
        # 크기를 고정하지 않습니다. 처음엔 이만큼을 원하되 작업 가능한 영역에
        # 맞춰 줄이고, 그 뒤로도 영역 밖으로 나가면 되돌립니다.
        self.keeper = ScreenKeeper.attach(self, self._preferred_size)

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

    def _preferred_size(self) -> QSize:
        """처음 열 때 원하는 크기. 격자 5일치와, 긴 안내 문구가 **한 줄로**
        들어가는 폭 중 큰 쪽. 실제로는 ScreenKeeper 가 작업 영역에 맞춰 줄이고,
        그때만 문구가 줄바꿈됩니다. 창이 떠서 레이아웃이 잡힌 뒤 불립니다."""
        pad = max(0, self.width() - self.hint.width())       # 탭 테두리·여백
        hol_btn = self.summary_btn.sizeHint().width() + 12   # 공휴일 탭 오른쪽 버튼 + 간격
        width = max(TIME_W + DAY_W * 5 + 80,
                    natural_width(self.hint) + pad,
                    natural_width(self.holiday_note) + hol_btn + pad)
        return QSize(width, 900)

    # -- eClass 과목 연결 ------------------------------------------------
    def _load_catalog(self) -> list[eclass_catalog.Entry]:
        """config 의 data_dir 아래 수집 기록 + course_repos + 수집 폴더.
        설정 전체 검증은 하지 않습니다(다른 설정이 틀려도 시간표는 고칠 수 있게)."""
        from .. import config as config_module

        try:
            raw = M.tomllib.loads(self.path.read_text(encoding="utf-8-sig")) \
                if self.path.exists() else {}
        except (OSError, M.tomllib.TOMLDecodeError, UnicodeDecodeError):
            raw = {}
        try:
            data_dir = config_module.Config(data=raw).data_dir
        except Exception:  # noqa: BLE001 — 경로를 못 구해도 config 쪽 목록은 씀
            data_dir = None
        repos = (raw.get("eclass", {}) or {}).get("course_repos", []) \
            if isinstance(raw.get("eclass"), dict) else []
        try:
            return eclass_catalog.gather(data_dir, repos)
        except OSError:
            return eclass_catalog.gather(None, repos)

    def fetch_course_list(self) -> bool:
        """eclass_probe.bat 로 과목 목록을 받아 옴(비밀번호가 없으면 set_password.bat 1)."""
        from .eclass_fetch import CourseListFetcher

        ok = CourseListFetcher(self, self.path).exec() == QDialog.DialogCode.Accepted
        self.catalog = self._load_catalog()
        return ok and bool(self.catalog)

    def link_courses(self) -> None:
        # 과목 목록이 없으면 안내 대신 바로 받아 옵니다.
        if not self.catalog and not self.fetch_course_list():
            self.statusBar().showMessage("eClass 과목 목록을 받지 못해 연결하지 않았습니다.", 6000)
            return
        dlg = LinkDialog(self, self.tt, self.catalog)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = dlg.result_timetable()
        if new is not None:
            self._apply(new, f"eClass 과목 {dlg.applied}개 연결")

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
        # 줄바꿈은 '영역이 모자랄 때만' 일어납니다. 처음 여는 폭은 이 문구가
        # 한 줄로 들어가게 잡고(_preferred_size), 영역보다 넓을 때만 접힙니다.
        # 줄바꿈을 끄면 최소 폭이 950px 로 묶여 작은 화면을 넘습니다.
        hint.setWordWrap(True)
        self.hint = hint
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
        note = QLabel("여기 적은 날짜의 수업은 녹화·송출 일정에서 건너뜁니다.")
        note.setWordWrap(True)
        top.addWidget(note, 1)
        self.holiday_note = note
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
        act(f, "디스크에서 다시 읽기", self.reload_from_disk, "F5")
        act(f, "설정 편집…", self.edit_settings, "Ctrl+,")
        act(f, "설치 · 점검 다시 실행…", self.run_setup_flow)
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
        e.addSeparator()
        act(e, "eClass 과목과 연결…", self.link_courses)

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
        got = CourseEditor(self, base, "수업 추가", self.catalog).exec()
        if got:
            new = self.tt.copy()
            new.courses.append(got)
            self._apply(new, f"{got.name} 추가")

    def edit_course(self, idx: int) -> None:
        if not (0 <= idx < len(self.tt.courses)):
            return
        got = CourseEditor(self, self.tt.courses[idx], "수업 수정", self.catalog).exec()
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
            return self.save(show_summary=False, setup=False)   # 닫는 중엔 창을 안 띄움
        return ans == QMessageBox.StandardButton.Discard

    def reload_from_disk(self) -> None:
        """config.toml 과 config.example.toml 을 둘 다 다시 읽습니다."""
        if not self._confirm_discard():
            return
        self._load_into_editor(report_errors=True)
        ex = (f"{self.example_path.name}(설정 {self.example_count}개)" if self.example_text
              else (self.example_error or "config.example.toml 없음"))
        if self.path.exists():
            self.statusBar().showMessage(f"{self.path.name} 과 {ex} 을 다시 읽었습니다.", 6000)

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
        self.catalog = self._load_catalog()
        self._load_example()
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

    def save(self, show_summary: bool = True, setup: bool | None = None) -> bool:
        """저장.
        분기 2(config.toml 없음): 설정 화면 → 만들기 → 시간표 저장 → 설치·점검.
        분기 1(config.toml 있음): 빠진 설정 확인(있으면 경고 + 설정 화면)
                                   → 시간표 저장 → 설치·점검.
        setup=False 면 설치·점검을 건너뜁니다(창을 닫으며 저장할 때 등)."""
        setup = self.SETUP_AFTER_SAVE if setup is None else setup
        if not self.path.exists():
            if not self._create_config():
                return False
        elif setup and not self.check_missing_settings():
            return False
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
        if setup:
            self.run_setup_flow()
        if show_summary:
            self.show_summary()
        return True

    # -- config.toml 이 없을 때 ------------------------------------------
    def _example_path(self) -> Path | None:
        """config.example.toml: 대상 파일 옆 → 레포 루트 순서로 찾음."""
        from .. import config as config_module

        repo_root = Path(config_module.__file__).resolve().parents[2]
        for cand in (self.path.with_name("config.example.toml"),
                     repo_root / "config.example.toml"):
            if cand.is_file():
                return cand
        return None

    def _load_example(self) -> None:
        """config.example.toml 을 읽어 둡니다(열 때, '디스크에서 다시 읽기' 때).
        설정 창은 이 읽어 둔 본문을 씁니다 — 예시 파일을 고쳤으면 다시 읽기로 반영."""
        self.example_path = self._example_path()
        self.example_text: str | None = None
        self.example_error = ""
        self.example_count = 0
        if self.example_path is None:
            return
        try:
            text = self.example_path.read_text(encoding="utf-8-sig")
            self.example_count = sum(len(s.fields) for s in settings_schema.parse(text))
            self.example_text = text
        except (OSError, UnicodeDecodeError, settings_schema.SchemaError) as exc:
            self.example_error = f"{self.example_path.name} 을 읽을 수 없습니다: {exc}"

    def _settings_dialog(self, base_text: str | None):
        from .config_wizard import SettingsDialog

        if self.example_text is None:
            why = self.example_error or "config.example.toml 을 찾지 못했습니다."
            QMessageBox.warning(self, "설정 목록 없음", why)
            return None
        return SettingsDialog(self, self.example_text, self.path, base_text,
                              example_name=self.example_path.name)

    def _create_config(self) -> bool:
        """config.toml 이 없으면 예시 파일로 설정을 고르게 한 뒤 만듭니다.
        True 면 계속 저장(시간표 블록을 그 뒤에 붙임)."""
        if self.example_text is None:
            why = self.example_error or "바탕으로 쓸 config.example.toml 도 찾지 못했습니다."
            ans = QMessageBox.question(
                self, "설정 파일 없음",
                f"{self.path.name} 이 없고, {why}\n"
                "시간표만 담긴 설정 파일을 만들까요? (다른 설정은 기본값으로 동작)")
            return ans == QMessageBox.StandardButton.Yes
        dlg = self._settings_dialog(None)
        if dlg is None or dlg.exec() != QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("설정 파일을 만들지 않아 저장하지 않았습니다.", 6000)
            return False
        M.write_text(self.path, dlg.result_text())
        self.statusBar().showMessage(
            f"{self.example_path.name} 을 바탕으로 {self.path.name} 을 만들었습니다.", 6000)
        return True

    def edit_settings(self) -> bool:
        """파일 → 설정 편집. 파일이 없으면 새로 만들기, 있으면 지금 값을 편집.
        시간표(편집 중인 것 포함)는 건드리지 않습니다."""
        if not self.path.exists():
            if not self._create_config():
                return False
            self._remember_disk()
            self.catalog = self._load_catalog()
            return True
        try:
            raw = self.path.read_bytes()
            base = raw.decode("utf-8-sig").replace("\r\n", "\n")
            dlg = self._settings_dialog(base)
        except (OSError, UnicodeDecodeError, settings_schema.SchemaError) as exc:
            QMessageBox.critical(self, "설정 파일을 읽을 수 없습니다", f"{self.path}\n{exc}")
            return False
        if dlg is None or dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        return self._write_settings(dlg, raw)

    def _write_settings(self, dlg, raw: bytes) -> bool:
        """설정 창 결과를 config.toml 에 씀(창을 연 사이 바뀌었으면 거부)."""
        if not dlg.changes():
            self.statusBar().showMessage("바뀐 설정이 없습니다.", 4000)
            return True
        if self.path.read_bytes() != raw:
            QMessageBox.warning(
                self, "다른 곳에서 바뀐 파일",
                f"설정 창을 연 사이 {self.path.name} 이 다른 곳에서 바뀌어 저장하지 않았습니다.\n"
                "다시 열어 고쳐 주세요.")
            return False
        M.write_text(self.path, dlg.result_text(), original=raw)
        self._remember_disk()                   # 내 저장을 바깥 변경으로 보지 않게
        self.catalog = self._load_catalog()     # data_dir·course_repos 가 바뀌었을 수 있음
        self.statusBar().showMessage(
            f"설정 {len(dlg.changes())}개를 {self.path.name} 에 저장했습니다 "
            f"(원본은 {self.path.name}.bak).", 6000)
        return True

    # -- 저장 분기 1: config.toml 이 있을 때 빠진 설정 찾기 ----------------
    def missing_settings(self) -> list[str]:
        """config.example.toml 에는 있는데 config.toml 에는 없는 설정(점 표기)."""
        if self.example_text is None or not self.path.exists():
            return []
        try:
            have = settings_schema.current_values(M.read_text(self.path))
            fields = [f for s in settings_schema.parse(self.example_text) for f in s.fields]
        except (OSError, UnicodeDecodeError, settings_schema.SchemaError):
            return []
        return [f.dotted for f in fields if f.dotted not in have]

    def check_missing_settings(self) -> bool:
        """빠진 설정이 있으면 경고와 함께 설정 화면(채워 넣기 모드)을 엽니다.
        False 면 사용자가 저장을 멈춤(취소)."""
        missing = self.missing_settings()
        if not missing:
            return True
        shown = ", ".join(missing[:6]) + (f" 외 {len(missing) - 6}개" if len(missing) > 6 else "")
        QMessageBox.warning(
            self, "빠진 설정",
            f"{self.path.name} 에 {self.example_path.name} 의 설정 {len(missing)}개가 "
            f"빠져 있습니다.\n  {shown}\n\n설정 화면을 엽니다. 저장하면 고친 값"
            "(안 고치면 예시 값)으로 채워 넣습니다.")
        try:
            raw = self.path.read_bytes()
            base = raw.decode("utf-8-sig").replace("\r\n", "\n")
            from .config_wizard import SettingsDialog

            dlg = SettingsDialog(self, self.example_text, self.path, base,
                                 example_name=self.example_path.name, fill_missing=True)
        except (OSError, UnicodeDecodeError, settings_schema.SchemaError) as exc:
            QMessageBox.critical(self, "설정 파일을 읽을 수 없습니다", f"{self.path}\n{exc}")
            return False
        if dlg.exec() != QDialog.DialogCode.Accepted:
            ans = QMessageBox.question(
                self, "빠진 설정",
                "빠진 설정을 채우지 않고 시간표만 저장할까요?")
            return ans == QMessageBox.StandardButton.Yes
        return self._write_settings(dlg, raw)

    # -- 저장 뒤 설치·점검 ------------------------------------------------
    def run_setup_flow(self) -> None:
        from .setup_flow import SetupFlow

        SetupFlow(self, self.path).exec()
        self._remember_disk()
        self.catalog = self._load_catalog()
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
