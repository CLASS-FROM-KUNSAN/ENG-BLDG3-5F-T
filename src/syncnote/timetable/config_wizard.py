"""config.toml 이 없을 때 config.example.toml 을 바탕으로 새로 만드는 창.

설정 목록·기본값·설명·분류는 전부 예시 파일에서 읽습니다(settings_schema).
이 창은 그것을 보기 좋게 배치하고, 사용자가 바꾼 값만 예시 본문에 바꿔
넣어 새 config.toml 본문을 돌려줍니다. 검사는 실제 설정 검증(config.problems)
그대로이고, 검사에 걸린 항목을 '먼저 확인할 항목' 으로 맨 위에 모읍니다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import settings_schema as S

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FIRST = "__first__"        # '먼저 확인할 항목' 트리 항목 표시
_BAD = object()            # 입력이 아직 값으로 안 읽힘(예: 잘못된 TOML)


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"' if value else '""(비어 있음)'
    return S.render(value)


class FieldRow(QFrame):
    """설정 한 줄: 제목 · 키 · 입력 · 설명 · 기본값으로."""

    def __init__(self, dlg: NewConfigDialog, f: S.Field) -> None:
        super().__init__()
        self.dlg, self.field = dlg, f
        self.setObjectName("fieldRow")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(3)

        head = QHBoxLayout()
        title = QLabel(f"<b>{_esc(f.title)}</b>")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        key = QLabel(f.dotted)
        key.setStyleSheet("color: #6B7280; font-family: Consolas, monospace;")
        key.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        head.addWidget(key)
        lay.addLayout(head)

        self.editor = self._make_editor()
        row = QHBoxLayout()
        row.addWidget(self.editor, 1)
        self.reset_btn = QPushButton("기본값으로")
        self.reset_btn.setToolTip(f"예시 값: {_fmt(f.value)}")
        self.reset_btn.clicked.connect(self.reset)
        row.addWidget(self.reset_btn)
        lay.addLayout(row)

        rest = "\n".join(f.doc.splitlines()[1:]).strip()
        if rest:
            help_ = QLabel(rest)
            help_.setWordWrap(True)
            help_.setStyleSheet("color: #6B7280;")
            lay.addWidget(help_)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #C0392B;")
        self.note.hide()
        lay.addWidget(self.note)
        self.set_value(dlg.values[f.dotted], silent=True)
        self.restyle()

    # -- 입력 위젯 -------------------------------------------------------
    def _make_editor(self) -> QWidget:
        k = self.field.kind
        if k == "bool":
            w = QCheckBox("켜기")
            w.toggled.connect(self._changed)
        elif k == "int":
            w = QSpinBox()
            w.setRange(-1_000_000_000, 1_000_000_000)
            w.valueChanged.connect(self._changed)
        elif k == "float":
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(3)
            w.valueChanged.connect(self._changed)
        elif k == "list":
            w = QPlainTextEdit()
            w.setPlaceholderText("한 줄에 하나씩")
            w.setFixedHeight(w.fontMetrics().lineSpacing() * 4 + 12)
            w.textChanged.connect(self._changed)
        else:   # str, other(표·섞인 목록) → 글자. other 는 TOML 값 그대로 적음
            w = QLineEdit()
            if k == "other":
                w.setPlaceholderText("TOML 값 그대로 (예: [1, 2] 또는 { a = 1 })")
            w.textChanged.connect(self._changed)
        return w

    def value(self) -> Any:
        k, w = self.field.kind, self.editor
        if k == "bool":
            return w.isChecked()
        if k in ("int", "float"):
            return w.value()
        if k == "list":
            items = [ln.strip() for ln in w.toPlainText().splitlines() if ln.strip()]
            sample = self.field.value
            if any(isinstance(x, (int, float)) for x in sample):
                try:
                    return [float(x) if "." in x else int(x) for x in items]
                except ValueError:
                    return _BAD
            if not sample:
                # 예시가 빈 목록이면 원소 종류를 모릅니다. 한 줄씩 TOML 값으로
                # 읽어 보고(2 → 숫자), 안 되면 글자로. 종류가 틀리면 설정 검사가 잡음.
                return [_scalar(x) for x in items]
            return items
        if k == "str":
            return w.text()
        try:
            return tomllib.loads(f"v = {w.text()}")["v"]
        except tomllib.TOMLDecodeError:
            return _BAD

    def set_value(self, v: Any, silent: bool = False) -> None:
        k, w = self.field.kind, self.editor
        w.blockSignals(silent)
        try:
            if k == "bool":
                w.setChecked(bool(v))
            elif k in ("int", "float"):
                w.setValue(v)
            elif k == "list":
                w.setPlainText("\n".join(str(x) for x in v))
            elif k == "str":
                w.setText(str(v))
            else:
                w.setText(S.render(v))
        finally:
            w.blockSignals(False)

    def _changed(self, *_a) -> None:
        self.dlg.on_edit(self.field, self.value())
        self.restyle()

    def reset(self) -> None:
        self.set_value(self.field.value)
        self._changed()

    # -- 표시 -----------------------------------------------------------
    def restyle(self) -> None:
        v = self.dlg.values.get(self.field.dotted)
        changed = v is not _BAD and v != self.field.value
        bad = v is _BAD
        issues = self.dlg.issues_for(self.field)
        color = "#C0392B" if (bad or issues) else ("#2F6FB5" if changed else "transparent")
        self.setStyleSheet(f"#fieldRow {{ border-left: 4px solid {color}; }}")
        self.reset_btn.setVisible(changed or bad)
        msgs = (["값을 읽을 수 없습니다(형식 확인)"] if bad else []) + issues
        self.note.setText("\n".join(f"⚠ {m}" for m in msgs))
        self.note.setVisible(bool(msgs))


def _scalar(text: str) -> Any:
    try:
        v = tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text
    return v if isinstance(v, (int, float, str, bool)) else text


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class NewConfigDialog(QDialog):
    def __init__(self, parent: QWidget | None, example: Path, target: Path) -> None:
        super().__init__(parent)
        from .gui import ScreenKeeper  # 순환 import 피함

        self.example, self.target = Path(example), Path(target)
        self.text = self.example.read_text(encoding="utf-8-sig")
        self.sections = S.parse(self.text)
        self.cats = S.categories(self.sections)
        self.fields = [f for s in self.sections for f in s.fields]
        self.by_dotted = {f.dotted: f for f in self.fields}
        self.values: dict[str, Any] = {f.dotted: f.value for f in self.fields}
        self.problem_list: list[str] = []
        self.rows: dict[str, FieldRow] = {}

        self.setWindowTitle(f"새 {self.target.name} 만들기")
        self.resize(980, 720)
        ScreenKeeper.attach(self)
        lay = QVBoxLayout(self)

        intro = QLabel(
            f"<b>{_esc(self.target.name)}</b> 이 없어 <b>{_esc(self.example.name)}</b> 의 설정 "
            f"{len(self.fields)}개를 불러왔습니다. 필요한 것만 고친 뒤 <b>만들기</b> 를 누르세요. "
            "고치지 않은 항목은 예시 값 그대로 들어가고, 설명 주석도 그대로 남습니다. "
            "비밀번호·토큰은 여기 적지 않습니다(<code>python -m syncnote secrets set</code>).")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("설정 찾기 (이름·설명·키)")
        self.search.textChanged.connect(lambda _t: self.show_selected())
        bar.addWidget(self.search, 1)
        self.only_changed = QCheckBox("바꾼 항목만")
        self.only_changed.toggled.connect(lambda _b: self.show_selected())
        bar.addWidget(self.only_changed)
        lay.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(lambda *_a: self.show_selected())
        split.addWidget(self.tree)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        split.addWidget(self.scroll)
        split.setStretchFactor(1, 3)
        split.setSizes([260, 700])
        lay.addWidget(split, 1)

        self.check_label = QLabel()
        lay.addWidget(self.check_label)
        self.check_list = QListWidget()
        self.check_list.itemActivated.connect(self._jump_to_problem)
        self.check_list.itemClicked.connect(self._jump_to_problem)
        lay.addWidget(self.check_list)

        bb = QDialogButtonBox()
        self.create_btn = bb.addButton("만들기", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._try_accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self.revalidate)

        self.revalidate()
        self._build_tree()
        first = self.tree.topLevelItem(0)
        self.tree.setCurrentItem(first)

    # -- 값 -------------------------------------------------------------
    def changes(self) -> dict[str, Any]:
        return {d: v for d, v in self.values.items()
                if v is not _BAD and v != self.by_dotted[d].value}

    def on_edit(self, f: S.Field, value: Any) -> None:
        self.values[f.dotted] = value
        self._timer.start()

    def result_text(self) -> str:
        return S.apply(self.text, self.changes())

    # -- 검사 -----------------------------------------------------------
    def revalidate(self) -> None:
        bad = [d for d, v in self.values.items() if v is _BAD]
        try:
            self.problem_list = S.problems(self.result_text())
        except S.SchemaError as exc:
            self.problem_list = [str(exc)]
        self.problem_list += [f"{d} 의 값을 읽을 수 없습니다" for d in bad]
        self.check_list.clear()
        for p in self.problem_list:
            it = QListWidgetItem(f"⚠ {p}")
            it.setForeground(Qt.GlobalColor.darkRed)
            self.check_list.addItem(it)
        n = len(self.problem_list)
        changed = len(self.changes())
        self.check_label.setText(
            (f"<b style='color:#C0392B'>설정 검사: 문제 {n}건</b> — 눌러서 해당 항목으로 이동"
             if n else "<b style='color:#15803D'>설정 검사: 문제 없음 ✔</b>")
            + f" · 바꾼 항목 {changed}개")
        self.check_list.setVisible(bool(n))
        line = self.check_list.fontMetrics().lineSpacing() + 6
        self.check_list.setFixedHeight(min(5, max(1, n)) * line + 6)
        for row in self.rows.values():
            row.restyle()
        self._update_tree_badges()

    def issues_for(self, f: S.Field) -> list[str]:
        return [p for p in self.problem_list if f in S.fields_in(p, self.fields)]

    def _first_fields(self) -> list[S.Field]:
        seen: list[S.Field] = []
        for p in self.problem_list:
            for f in S.fields_in(p, self.fields):
                if f not in seen:
                    seen.append(f)
        return seen

    # -- 왼쪽 트리 ------------------------------------------------------
    def _build_tree(self) -> None:
        self.tree.clear()
        first = QTreeWidgetItem([""])
        first.setData(0, Qt.ItemDataRole.UserRole, FIRST)
        self.tree.addTopLevelItem(first)
        for cat in self.cats:
            top = QTreeWidgetItem([cat.title])
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setData(0, Qt.ItemDataRole.UserRole, ("cat", cat.top))
            self.tree.addTopLevelItem(top)
            for sec in cat.sections:
                child = QTreeWidgetItem([sec.title if sec.path != cat.top else "기본"])
                child.setToolTip(0, f"[{sec.path}]")
                child.setData(0, Qt.ItemDataRole.UserRole, ("sec", sec.path))
                top.addChild(child)
            top.setExpanded(cat.top in ("general", "eclass", "github"))
        self._update_tree_badges()

    def _update_tree_badges(self) -> None:
        if self.tree.topLevelItemCount() == 0:
            return
        first = self.tree.topLevelItem(0)
        n = len(self._first_fields())
        first.setText(0, f"먼저 확인할 항목 ({n})" if n else "먼저 확인할 항목 — 없음 ✔")
        ch = self.changes()
        for i in range(1, self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            _k, top_name = top.data(0, Qt.ItemDataRole.UserRole)
            k = sum(1 for d in ch if d.split(".")[0] == top_name)
            cat = next(c for c in self.cats if c.top == top_name)
            top.setText(0, cat.title + (f"  · 바꿈 {k}" if k else ""))

    # -- 오른쪽 양식 ----------------------------------------------------
    def _visible_fields(self) -> list[tuple[str, list[S.Field]]]:
        """(제목, 항목들) 묶음 목록. 검색어가 있으면 전체에서 찾습니다."""
        q = self.search.text().strip().lower()
        only = self.only_changed.isChecked()
        ch = self.changes()

        def keep(f: S.Field) -> bool:
            if only and f.dotted not in ch:
                return False
            return not (q and q not in f"{f.dotted}\n{f.doc}".lower())

        if q or only:
            return [(f"{s.title}  [{s.path}]", [f for f in s.fields if keep(f)])
                    for s in self.sections if any(keep(f) for f in s.fields)]
        item = self.tree.currentItem()
        tag = item.data(0, Qt.ItemDataRole.UserRole) if item else FIRST
        if tag == FIRST:
            fs = self._first_fields()
            return [("먼저 확인할 항목 — 설정 검사에 걸린 것", fs)] if fs else []
        kind, name = tag
        secs = [s for s in self.sections if (s.top == name if kind == "cat" else s.path == name)]
        return [(f"{s.title}  [{s.path}]", s.fields) for s in secs]

    def show_selected(self) -> None:
        body = QWidget()
        lay = QVBoxLayout(body)
        self.rows = {}
        blocks = self._visible_fields()
        if not blocks:
            msg = ("설정 검사에 걸린 항목이 없습니다. 바로 만들어도 됩니다."
                   if not (self.search.text().strip() or self.only_changed.isChecked())
                   else "찾는 항목이 없습니다.")
            lab = QLabel(msg)
            lab.setStyleSheet("color: #6B7280;")
            lay.addWidget(lab)
        for title, fields in blocks:
            head = QLabel(f"<h3 style='margin:8px 0 2px 0'>{_esc(title)}</h3>")
            lay.addWidget(head)
            sec = next((s for s in self.sections if f"[{s.path}]" in title), None)
            if sec is not None and sec.doc:
                d = QLabel(sec.doc)
                d.setWordWrap(True)
                d.setStyleSheet("color: #6B7280;")
                lay.addWidget(d)
            group = None
            for f in fields:
                if f.group and f.group != group:
                    g = QLabel(f"— {f.group} —")
                    g.setStyleSheet("color: #2F6FB5; font-weight: bold; margin-top: 6px;")
                    lay.addWidget(g)
                group = f.group
                row = FieldRow(self, f)
                self.rows[f.dotted] = row
                lay.addWidget(row)
        lay.addStretch(1)
        self.scroll.setWidget(body)

    def _jump_to_problem(self, item: QListWidgetItem) -> None:
        idx = self.check_list.row(item)
        if not (0 <= idx < len(self.problem_list)):
            return
        hits = S.fields_in(self.problem_list[idx], self.fields)
        self.search.clear()
        self.only_changed.setChecked(False)
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        if hits and hits[0].dotted in self.rows:
            row = self.rows[hits[0].dotted]
            self.scroll.ensureWidgetVisible(row)
            row.editor.setFocus()

    # -- 만들기 ---------------------------------------------------------
    def _try_accept(self) -> None:
        self._timer.stop()
        self.revalidate()
        try:
            self.result_text()
        except S.SchemaError as exc:
            QMessageBox.critical(self, "만들 수 없습니다", str(exc))
            return
        if self.problem_list:
            ans = QMessageBox.question(
                self, "설정 검사 문제",
                f"설정 검사에서 문제 {len(self.problem_list)}건이 남아 있습니다.\n"
                "그대로 만들면 녹화·수집을 실행할 때 이 문제로 멈춥니다. 나중에 "
                f"{self.target.name} 을 직접 고쳐도 됩니다.\n\n그래도 만들까요?")
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.accept()
