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

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer
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

from .. import settings_knowledge as K
from .. import settings_schema as S

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

FIRST = "__first__"        # '먼저 확인할 항목' 트리 항목 표시
# 켬/끔 설명이 없을 때 보여 줄 안내(예시 파일만 고치면 바로 반영됨을 알려 줌)
TOGGLE_HOWTO = ("이 스위치는 {name} 에 켬/끔 설명이 없습니다. {name} 에서 <code>{key}</code> "
                "바로 위에 아래 두 줄을 적고 <b>파일 → 디스크에서 다시 읽기</b> 를 누르면 "
                "여기에 바로 나타납니다(이 창이 열려 있으면 위 알림의 <b>새 설명 불러오기</b>).<br>"
                "<code>#   true  = 켬 — 켜면 일어나는 일</code><br>"
                "<code>#   false = 끔 — 끄면 일어나는 일</code>")
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
        self.reset_btn = QPushButton("예시 값으로")
        self.reset_btn.setToolTip(f"config.example.toml 의 값: {_fmt(f.value)}")
        self.reset_btn.clicked.connect(self.reset)
        row.addWidget(self.reset_btn)
        lay.addLayout(row)

        self.toggle_label: QLabel | None = None
        if f.kind == "bool":
            self.tg = S.toggle(f)
            self.toggle_label = QLabel()
            self.toggle_label.setWordWrap(True)
            self.toggle_label.setTextFormat(Qt.TextFormat.RichText)
            lay.addWidget(self.toggle_label)
        rest = S.help_without_toggle(f)
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
        # 값이 어긋나면 '읽을 수 없습니다' 대신: 원문 · 코드가 아는 설정 · 추측 · 교차검증 부탁
        self.mismatch = QLabel()
        self.mismatch.setWordWrap(True)
        self.mismatch.setTextFormat(Qt.TextFormat.RichText)
        self.mismatch.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.mismatch.setStyleSheet("color: #7C2D12; background: #FFF7ED; padding: 6px;")
        self.mismatch.hide()
        lay.addWidget(self.mismatch)
        self.fix_btn = QPushButton()
        self.fix_btn.hide()
        self.fix_btn.clicked.connect(self._apply_suggestion)
        lay.addWidget(self.fix_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._suggestion: Any = K.MISSING
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

    def _fit(self, v: Any) -> Any:
        """입력 칸 종류와 다른 값(예: 숫자 칸에 "10")은 그 종류로 읽어 보이고, 안 되면 예시 값."""
        like = self.field.value
        if K.kind_ko(v) == K.kind_ko(like):
            return v
        c = K.coerce(v, like)
        return like if c is K.MISSING else c

    def set_value(self, v: Any, silent: bool = False) -> None:
        k, w = self.field.kind, self.editor
        w.blockSignals(silent)
        try:
            if k == "bool":
                w.setChecked(bool(self._fit(v)))
            elif k == "int":
                w.setValue(int(self._fit(v)))
            elif k == "float":
                w.setValue(float(self._fit(v)))
            elif k == "list":
                w.setPlainText("\n".join(str(x) for x in self._fit(v)))
            elif k == "str":
                w.setText(str(v))
            else:
                w.setText(S.render(v))
        finally:
            w.blockSignals(False)

    def _changed(self, *_a) -> None:
        self.dlg.on_edit(self.field, self.value())
        self.restyle()

    def _render_toggle(self) -> None:
        """켬/끔 설명 두 줄. 지금 상태 쪽을 굵게·색으로, 예시 파일의 [기본] 을 (기본) 으로."""
        if self.toggle_label is None:
            return
        tg = self.tg
        if not tg.complete:
            self.toggle_label.setStyleSheet("color: #92400E; background: #FEF3C7; padding: 4px;")
            self.toggle_label.setText(TOGGLE_HOWTO.format(name=_esc(self.dlg.example_name),
                                                          key=_esc(self.field.key)))
            return
        self.toggle_label.setStyleSheet("")
        on_now = bool(self.editor.isChecked())

        def line(word: str, desc: str, is_on: bool) -> str:
            mark = " <span style='color:#6B7280'>(기본)</span>" if tg.default is is_on else ""
            if is_on == on_now:
                color = "#15803D" if is_on else "#B45309"
                return (f"<div style='color:{color}'><b>● {word} — {_esc(desc)}</b>{mark}</div>")
            return f"<div style='color:#6B7280'>○ {word} — {_esc(desc)}{mark}</div>"
        self.toggle_label.setText(line("켬", tg.on, True) + line("끔", tg.off, False))

    def reset(self) -> None:
        self.set_value(self.field.value)
        self._changed()

    def _apply_suggestion(self) -> None:
        if self._suggestion is K.MISSING:
            return
        self.set_value(self._suggestion)
        self._changed()

    def typed_text(self) -> str:
        w = self.editor
        if isinstance(w, QPlainTextEdit):
            return w.toPlainText()
        return w.text() if isinstance(w, QLineEdit) else ""

    # -- 표시 -----------------------------------------------------------
    def restyle(self) -> None:
        v = self.dlg.values.get(self.field.dotted)
        changed = v is not _BAD and (v != self.dlg.base_value(self.field)
                                      or (self.dlg.fill_missing
                                          and self.field.dotted in self.dlg.missing))
        bad = v is _BAD
        off_default = v is not _BAD and v != self.field.value
        issues = self.dlg.issues_for(self.field)
        rep = self.dlg.report_for(self.field, self)
        color = ("#C0392B" if (bad or issues) else "#EA580C" if rep
                 else "#2F6FB5" if changed else "transparent")
        self.setStyleSheet(f"#fieldRow {{ border-left: 4px solid {color}; }}")
        self.reset_btn.setVisible(off_default or bad)
        self._render_toggle()
        self.note.hide()
        self.mismatch.setVisible(bool(rep))
        self._suggestion = rep.suggestion if rep else K.MISSING
        if rep:
            self.mismatch.setText(K.html(rep))
        show_fix = self._suggestion is not K.MISSING and self._suggestion != v
        self.fix_btn.setVisible(show_fix)
        if show_fix:
            self.fix_btn.setText(f"추측한 값 {K.show(self._suggestion)} 넣어 보기 "
                                 f"({rep.suggestion_why} — 확인 후 저장)")


def _scalar(text: str) -> Any:
    try:
        v = tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text
    return v if isinstance(v, (int, float, str, bool)) else text


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SettingsDialog(QDialog):
    """설정 창. base_text 가 없으면 '새로 만들기', 있으면 '기존 설정 편집'.

    항목 목록·설명·분류는 예시 본문(example_text)에서, 지금 값은 base_text
    에서 읽습니다. 예시에 없는 키(손으로 더한 것)는 화면에 없지만 그대로
    남습니다. 결과는 base(없으면 예시) 본문에 바꾼 값만 넣은 것입니다.
    """

    def __init__(self, parent: QWidget | None, example_text: str, target: Path,
                 base_text: str | None = None,
                 example_name: str = "config.example.toml",
                 fill_missing: bool = False, example_path: Path | None = None,
                 base_note: str = "") -> None:
        super().__init__(parent)
        from .gui import ScreenKeeper  # 순환 import 피함

        self.target = Path(target)
        self.example_name = example_name
        self.text = example_text
        self.edit_mode = base_text is not None
        self.base_original = base_text
        # 깨진 곳이 있어도 '읽을 수 없습니다' 로 멈추지 않습니다 — 깨진 곳만 빼고 읽고 알림.
        self._load_example()
        self.base_note = base_note
        self.base_broken: list[S.Broken] = []
        if self.edit_mode:
            self.base_vals, self.base_broken, self.base = S.current_values_lenient(base_text)
        else:
            self.base_vals = {}
            self.base = S.salvage(example_text)[0]
        # 깨진 config.toml·다른 인코딩은 바뀐 값이 없어도 저장해야 고쳐집니다
        self.repaired = bool(self.base_broken or base_note)
        # 편집 모드에서 이 파일에 없는 설정. fill_missing 이면 안 건드려도 예시 값으로 넣음.
        self.missing = [f.dotted for f in self.fields
                        if self.edit_mode and f.dotted not in self.base_vals]
        self.fill_missing = fill_missing and bool(self.missing)
        self.values: dict[str, Any] = {f.dotted: self.base_value(f) for f in self.fields}
        self.problem_list: list[str] = []
        self.bad_msgs: list[str] = []
        self.rows: dict[str, FieldRow] = {}

        self.setWindowTitle(f"설정 편집 — {self.target.name}" if self.edit_mode
                            else f"새 {self.target.name} 만들기")
        self.resize(980, 720)
        ScreenKeeper.attach(self)
        lay = QVBoxLayout(self)

        missing = sum(1 for f in self.fields if f.dotted not in self.base_vals)
        if self.fill_missing:
            msg = (f"⚠ <b>{_esc(self.target.name)}</b> 에 <b>{_esc(example_name)}</b> 의 설정 "
                   f"<b>{len(self.missing)}개</b>가 빠져 있습니다. 맨 위 '먼저 확인할 항목' 에 모았고, "
                   "<b>저장</b> 하면 고친 값(안 고치면 예시 값)으로 채워 넣습니다. "
                   "다른 줄·주석·시간표는 그대로 둡니다. ")
        elif self.edit_mode:
            msg = (f"<b>{_esc(self.target.name)}</b> 의 지금 값을 보여 줍니다. 항목과 설명은 "
                   f"<b>{_esc(example_name)}</b> 에서 읽었습니다(설정 {len(self.fields)}개"
                   + (f", 이 파일에 없는 항목 {missing}개는 예시 값으로 표시" if missing else "")
                   + "). 저장하면 바꾼 줄만 고치고 주석·시간표는 그대로 둡니다. ")
        else:
            msg = (f"<b>{_esc(self.target.name)}</b> 이 없어 <b>{_esc(example_name)}</b> 의 설정 "
                   f"{len(self.fields)}개를 불러왔습니다. 필요한 것만 고친 뒤 <b>만들기</b> 를 "
                   "누르세요. 고치지 않은 항목은 예시 값 그대로 들어가고, 설명 주석도 그대로 남습니다. ")
        intro = QLabel(msg + "비밀번호·토큰은 여기 적지 않습니다"
                       "(<code>python -m syncnote secrets set</code>).")
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.broken_banner = QLabel()
        self.broken_banner.setWordWrap(True)
        self.broken_banner.setTextFormat(Qt.TextFormat.RichText)
        self.broken_banner.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.broken_banner.setStyleSheet("color: #92400E; background: #FEF3C7; padding: 6px;")
        lay.addWidget(self.broken_banner)
        self._show_broken()

        # 예시 파일이 바뀌면(켬/끔 설명·항목 추가 등) 이 창에서 바로 반영할 수 있게 알림
        self.example_path = Path(example_path) if example_path else None
        self._pending_example: str | None = None
        banner = QHBoxLayout()
        self.example_banner = QLabel()
        self.example_banner.setWordWrap(True)
        self.example_banner.setStyleSheet("color: #1E3A8A; background: #DBEAFE; padding: 6px;")
        banner.addWidget(self.example_banner, 1)
        self.reload_btn = QPushButton("새 설명 불러오기")
        self.reload_btn.clicked.connect(self.reload_example)
        banner.addWidget(self.reload_btn)
        lay.addLayout(banner)
        self.example_banner.hide()
        self.reload_btn.hide()
        self._watch = None
        if self.example_path is not None and self.example_path.exists():
            self._watch = QFileSystemWatcher([str(self.example_path)], self)
            self._watch.fileChanged.connect(self._example_changed)

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
        self.create_btn = bb.addButton("저장" if self.edit_mode else "만들기",
                                       QDialogButtonBox.ButtonRole.AcceptRole)
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

    # -- 예시 읽기 + 코드와 어긋남 ----------------------------------------
    def _load_example(self) -> None:
        """예시 본문을 깨진 곳만 빼고 읽고, 깨진 줄 때문에 빠진 설정 중 코드가 아는 것은
        코드 기본값으로 자리를 만들어 둡니다(그 상세 페이지에서 원문·근거·추측을 보여 줌)."""
        self.sections, self.example_broken = S.parse_lenient(self.text)
        self.example_keys = {f.dotted for s in self.sections for f in s.fields}
        self.ghost: dict[str, S.Broken] = {}
        known = K.code_keys()
        by_path = {s.path: s for s in self.sections}
        for b in self.example_broken:
            names: list[str] = []
            for d in b.dropped:
                if d.endswith(" 섹션 전체") and d.startswith("["):
                    sec = d[1:d.index("]")]
                    names += [k for k in known if k.rpartition(".")[0] == sec]
                else:
                    names.append(d)
            for d in names:
                if d in self.example_keys or d in self.ghost or d not in known:
                    continue
                sec, _, key = d.rpartition(".")
                section = by_path.get(sec)
                if section is None:
                    section = S.Section(sec, f"[{sec}] — 예시 파일에서 읽지 못함", line=b.line)
                    self.sections.append(section)
                    by_path[sec] = section
                section.fields.append(S.Field(
                    sec, key, K.knowledge(d).default,
                    f"{key} — ⚠ {self.example_name} {b.line}번째 줄을 읽지 못해 코드 기본값으로 "
                    "보여 줍니다", line=b.line))
                section.fields.sort(key=lambda f: f.line)
                self.ghost[d] = b
        self.cats = S.categories(self.sections)
        self.fields = [f for s in self.sections for f in s.fields]
        self.by_dotted = {f.dotted: f for f in self.fields}

    def report_for(self, f: S.Field, row: FieldRow | None = None) -> K.Report:
        """이 설정이 예시·config.toml·코드 사이에서 어긋난 것(없으면 빈 보고 = False)."""
        origs: list[K.Original] = []
        broken: list[tuple[str, S.Broken]] = []
        b = self.ghost.get(f.dotted)
        o = (K.original(self.example_name, self.text, b.line, b.end) if b
             else K.original(self.example_name, self.text, f.line) if f.line else None)
        if o:
            origs.append(o)
        if b:
            broken.append((self.example_name, b))
        if self.edit_mode and self.base_original is not None:
            cb = next((x for x in self.base_broken if f.dotted in x.dropped), None)
            if cb is not None:
                broken.append((self.target.name, cb))
                o = K.original(self.target.name, self.base_original, cb.line, cb.end)
            else:
                ln = K.locate(self.base_original, f.dotted)
                o = K.original(self.target.name, self.base_original, ln) if ln else None
            if o:
                origs.append(o)
        v = self.values.get(f.dotted, K.MISSING)
        typed = None
        if v is _BAD:
            typed = row.typed_text() if row is not None else ""
            v = K.MISSING
        cur = v if self.edit_mode else K.MISSING
        issues = [p for p in self.issues_for(f) if p not in self.bad_msgs]
        return K.analyze(f.dotted, example_value=K.MISSING if b else f.value,
                         example_keys=self.example_keys, current_value=cur, typed=typed,
                         issues=issues, broken=broken, shown=v, originals=origs)

    # -- 깨진 곳 알림 -----------------------------------------------------
    @staticmethod
    def _broken_list(items: list[S.Broken], limit: int = 6) -> str:
        rows = "".join(f"<li>{_esc(b.describe())}</li>" for b in items[:limit])
        more = f"<li>… 외 {len(items) - limit}곳</li>" if len(items) > limit else ""
        return f"<ul style='margin:2px 0 0 -20px'>{rows}{more}</ul>"

    def _show_broken(self) -> None:
        parts = []
        if self.example_broken:
            parts.append(
                f"<b>{_esc(self.example_name)}</b> 에서 {len(self.example_broken)}곳을 읽지 못해 "
                f"<b>그 부분만 빼고</b> 불러왔습니다(나머지 설정 {len(self.fields)}개는 정상)."
                + self._broken_list(self.example_broken)
                + "그 줄을 고쳐 저장하면 이 창 위에 '새 설명 불러오기' 가 뜹니다.")
        if self.base_broken:
            parts.append(
                f"<b>{_esc(self.target.name)}</b> 에서 {len(self.base_broken)}곳을 읽지 못했습니다. "
                "나머지 값은 그대로 보여 줍니다. <b>저장</b> 하면 이 줄들은 원문 그대로 "
                f"<code>{_esc(S.BROKEN_MARK.strip())}</code> 주석으로 남기고 파일을 다시 읽을 수 "
                f"있게 고칩니다(원래 파일은 {_esc(self.target.name)}.bak). 빠진 설정은 예시 값으로 "
                "보이니, 필요하면 여기서 다시 정하세요." + self._broken_list(self.base_broken))
        if self.base_note:
            parts.append(_esc(self.base_note))
        self.broken_banner.setText("<br>".join(parts))
        self.broken_banner.setVisible(bool(parts))

    # -- 예시 파일이 바뀜 -------------------------------------------------
    def _example_changed(self, *_a) -> None:
        """예시 파일이 바뀌었으면 알림만 띄웁니다(입력 중인 값을 갑자기 바꾸지 않음)."""
        if self._watch is not None and str(self.example_path) not in self._watch.files():
            self._watch.addPath(str(self.example_path))   # 편집기는 지우고 새로 쓰기도 함
        try:
            new = self.example_path.read_text(encoding="utf-8-sig")
        except OSError:
            return
        if new == self.text:
            return
        try:
            secs, broken = S.parse_lenient(new)
        except S.SchemaError as exc:              # 깨진 곳이 너무 많을 때만
            self._pending_example = None
            self.example_banner.setText(
                f"<b>{_esc(self.example_name)}</b> 이 바뀌었는데 {_esc(str(exc))}<br>"
                "그 파일을 고쳐 저장하면 다시 알려 드립니다. 지금 화면은 그대로 쓸 수 있습니다.")
            self.example_banner.show()
            self.reload_btn.hide()
            return
        n_new = sum(len(s.fields) for s in secs)
        self._pending_example = new
        tail = ("" if not broken else
                f"<br>⚠ {len(broken)}곳은 읽지 못해 <b>그 부분만 빼고</b> 불러옵니다."
                + self._broken_list(broken, limit=3))
        self.example_banner.setText(
            f"<b>{_esc(self.example_name)}</b> 이 바뀌었습니다(설정 {len(self.fields)}개 → {n_new}개). "
            "<b>새 설명 불러오기</b> 를 누르면 바뀐 항목·켬/끔 설명을 이 창에 반영합니다. "
            "지금까지 고친 값은 그대로 둡니다." + tail)
        self.example_banner.show()
        self.reload_btn.show()

    def reload_example(self) -> None:
        """새 예시 본문으로 항목·설명을 다시 만듭니다. 사용자가 고친 값은 유지."""
        new = self._pending_example
        if new is None:
            return
        keep = dict(self.values)
        self.text = new
        if not self.edit_mode:
            self.base = S.salvage(new)[0]
        self._load_example()
        self.missing = [f.dotted for f in self.fields
                        if self.edit_mode and f.dotted not in self.base_vals]
        self.values = {f.dotted: keep.get(f.dotted, self.base_value(f)) for f in self.fields}
        self._pending_example = None
        self._build_tree()
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.revalidate()
        self.show_selected()
        self.reload_btn.hide()
        self._show_broken()
        self.example_banner.setText(
            f"새 설명을 불러왔습니다(설정 {len(self.fields)}개). 고친 값은 그대로입니다.")

    # -- 값 -------------------------------------------------------------
    def base_value(self, f: S.Field) -> Any:
        """지금 파일의 값(파일에 없으면 예시 값)."""
        return self.base_vals.get(f.dotted, f.value)

    def changes(self) -> dict[str, Any]:
        return {d: v for d, v in self.values.items()
                if v is not _BAD and (v != self.base_value(self.by_dotted[d])
                                      or (self.fill_missing and d in self.missing))}

    def on_edit(self, f: S.Field, value: Any) -> None:
        self.values[f.dotted] = value
        self._timer.start()

    def result_text(self) -> str:
        return S.apply(self.base, self.changes(), known=set(self.by_dotted))

    # -- 검사 -----------------------------------------------------------
    def revalidate(self) -> None:
        bad = [d for d, v in self.values.items() if v is _BAD]
        try:
            self.problem_list = S.problems(self.result_text())
        except S.SchemaError as exc:
            self.problem_list = [str(exc)]
        self.bad_msgs = [f"{d} 에 넣은 값이 코드가 기다리는 형식과 어긋납니다 — 그 항목의 설명을 보세요"
                         for d in bad]
        self.problem_list += self.bad_msgs
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
        if self.fill_missing:                 # 빠진 설정이 먼저
            seen += [self.by_dotted[d] for d in self.missing]
        for p in self.problem_list:
            for f in S.fields_in(p, self.fields):
                if f not in seen:
                    seen.append(f)
        for f in self.fields:                 # 예시·config.toml·코드가 서로 어긋난 것
            if f not in seen and self.report_for(f, self.rows.get(f.dotted)):
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
            return [("먼저 확인할 항목 — 설정 검사에 걸렸거나 코드와 어긋난 것", fs)] if fs else []
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


class NewConfigDialog(SettingsDialog):
    """예시 파일 경로로 '새로 만들기' 창을 여는 지름길."""

    def __init__(self, parent: QWidget | None, example: Path, target: Path) -> None:
        example = Path(example)
        super().__init__(parent, example.read_text(encoding="utf-8-sig"), target,
                         example_name=example.name, example_path=example)
        self.example = example
