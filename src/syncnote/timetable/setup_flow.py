"""저장 뒤 '설치 → 라이브러리 확인 → 비밀번호 → 환경 점검' 을 차례로 돌리는 창.

시간표 편집기에서 저장하면(설정 확인·저장이 끝난 뒤) 이 창이 이어서 엽니다.

  1. 설치        scripts/setup.bat 을 실행(가상환경·라이브러리·브라우저·ffmpeg).
                 출력은 이 창에 보이고, 끝나면 다음 단계로 넘어갑니다(bat 의
                 마지막 pause 는 입력을 닫아 기다리지 않게 함).
  2. 라이브러리  requirements.txt 와 실제 설치(.venv)를 맞춰 보고(syncnote.reqcheck),
                 빠졌거나 버전이 안 맞는 줄만 pip install 한 뒤 다시 확인.
  3. 비밀번호    eClass 비밀번호가 OS 자격증명 저장소(keyring)에 있는지 확인하고,
                 없으면 scripts/set_password.bat 1 을 새 콘솔 창에 엽니다.
  4. 점검        scripts/doctor.bat --json, scripts/devices.bat --json 결과를 그림으로.

setup.bat 이 끝에 텍스트로 안내하던 '다음 순서'(설정 → 비밀번호 → 점검 → 오디오
장치)는 결과 화면 맨 위에 '했는지/안 했는지' 체크 목록으로 바뀝니다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import setupcheck
from . import eclass_fetch

REPO_ROOT = eclass_fetch.REPO_ROOT
SCRIPTS = eclass_fetch.SCRIPTS

STEPS = (
    ("install", "1. 설치 (setup.bat)"),
    ("verify", "2. 라이브러리 확인 (requirements.txt)"),
    ("password", "3. eClass 비밀번호"),
    ("check", "4. 환경 점검 · 오디오 장치"),
)
ICON = {"wait": "○", "run": "⏳", "ok": "✔", "warn": "⚠", "fail": "✖", "skip": "–"}
COLOR = {"wait": "#6B7280", "run": "#2F6FB5", "ok": "#15803D", "warn": "#B45309",
         "fail": "#C0392B", "skip": "#6B7280"}
MARK = {"O": ("✔", "#15803D"), "X": ("✖", "#C0392B"), "-": ("–", "#6B7280"),
        "!": ("⚠", "#B45309")}


def last_json(text: str) -> Any:
    """출력에서 마지막 JSON 한 줄(bat 의 echo·pause 줄은 건너뜀)."""
    for line in reversed(text.replace("\r\n", "\n").splitlines()):
        s = line.strip()
        if s.startswith(("{", "[")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def _esc(s: Any) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SetupFlow(QDialog):
    def __init__(self, parent: QWidget | None, config_path: Path) -> None:
        super().__init__(parent)
        from .gui import ScreenKeeper

        self.config_path = Path(config_path)
        self.setWindowTitle("설치 · 점검")
        self.resize(900, 640)
        ScreenKeeper.attach(self)

        self.state = {k: "wait" for k, _ in STEPS}
        self.notes: dict[str, str] = {}
        self.reqs: list[dict] = []
        self.secret_status: dict[str, str] = {}
        self.doctor: dict | None = None
        self.devices: dict | None = None
        self.proc: QProcess | None = None
        self.pw_proc: subprocess.Popen | None = None
        self._queue: list[str] = []
        self._pw_rounds = 0
        self.install_state: dict | None = None
        self.setup_rc = 0

        lay = QVBoxLayout(self)
        self.headline = QLabel("저장한 설정으로 설치와 점검을 이어서 진행합니다.")
        self.headline.setWordWrap(True)
        lay.addWidget(self.headline)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.step_list = QListWidget()
        self.step_list.setWordWrap(True)
        for key, title in STEPS:
            it = QListWidgetItem(f"{ICON['wait']}  {title}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.step_list.addItem(it)
        split.addWidget(self.step_list)
        self.stack = QStackedWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.stack.addWidget(self.log)
        self.result_scroll = QScrollArea()
        self.result_scroll.setWidgetResizable(True)
        self.stack.addWidget(self.result_scroll)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 3)
        split.setSizes([240, 640])
        lay.addWidget(split, 1)

        bb = QDialogButtonBox()
        self.log_btn = QPushButton("출력 보기")
        self.log_btn.setCheckable(True)
        self.log_btn.toggled.connect(lambda on: self.stack.setCurrentIndex(0 if on else 1))
        self.log_btn.setEnabled(False)
        bb.addButton(self.log_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.close_btn = QPushButton("중단")
        self.close_btn.clicked.connect(self.reject)
        bb.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        lay.addWidget(bb)

        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._check_password_window)

    # -- 실행할 명령(테스트에서 바꿔 끼움) ---------------------------------
    def venv_path(self) -> Path:
        return REPO_ROOT / ".venv" / "Scripts" / "python.exe"

    def venv_exists(self) -> bool:
        return self.venv_path().exists()

    def venv_python(self) -> str:
        return str(self.venv_path()) if self.venv_exists() else sys.executable

    def state_command(self) -> list[str]:
        """설치가 이미 됐는지(setupcheck) — .venv 의 파이썬으로 봐야 그 환경을 봄."""
        return [str(self.venv_path()), "-m", "syncnote.setupcheck",
                str(REPO_ROOT / "requirements.txt")]

    def setup_command(self) -> list[str] | None:
        if os.name != "nt":
            return None                       # bat 은 Windows 전용
        return ["cmd.exe", "/d", "/c", str(SCRIPTS / "setup.bat")]

    def reqcheck_command(self) -> list[str]:
        return [self.venv_python(), "-m", "syncnote.reqcheck", str(REPO_ROOT / "requirements.txt")]

    def pip_install_command(self, lines: list[str]) -> list[str]:
        return [self.venv_python(), "-m", "pip", "install", *lines]

    def status_command(self) -> list[str]:
        return [self.venv_python(), "-c",
                "import json; from syncnote import secrets; "
                "print(json.dumps(secrets.status(), ensure_ascii=False))"]

    def password_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "set_password.bat"), "1"]
        return [self.venv_python(), "-m", "syncnote", "secrets", "set", "eclass"]

    def doctor_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "doctor.bat"), "--json"]
        return [self.venv_python(), "-m", "syncnote", "doctor", "--json"]

    def devices_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "devices.bat"), "--json"]
        return [self.venv_python(), "-m", "syncnote", "devices", "--json"]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SYNCNOTE_CONFIG"] = str(self.config_path)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    # -- 프로세스 하나 돌리기 ---------------------------------------------
    def _run(self, cmd: list[str], done: Callable[[int, str], None]) -> None:
        self._say(f"> {' '.join(cmd)}")
        p = QProcess(self)
        p.setWorkingDirectory(str(REPO_ROOT))
        env = QProcessEnvironment()
        for k, v in self._env().items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        buf: list[str] = []

        def read() -> None:
            chunk = bytes(p.readAllStandardOutput()).decode("utf-8", errors="replace")
            buf.append(chunk)
            for line in chunk.replace("\r\n", "\n").splitlines():
                if line.strip() and not line.lstrip().startswith(("{", "[")):
                    self._say(line)

        def finished(code: int, _status) -> None:
            read()
            if self.proc is p:
                self.proc = None
            done(int(code), "".join(buf))

        def failed(err) -> None:
            if err == QProcess.ProcessError.FailedToStart:
                self._say(f"! 실행하지 못했습니다: {p.errorString()}")
                if self.proc is p:
                    self.proc = None
                done(-1, "")

        p.readyReadStandardOutput.connect(read)
        p.finished.connect(finished)
        p.errorOccurred.connect(failed)
        self.proc = p
        p.start(cmd[0], cmd[1:])
        p.closeWriteChannel()             # bat 끝의 pause 가 기다리지 않게

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip("\n"))

    # -- 단계 진행 --------------------------------------------------------
    def start(self) -> None:
        self._queue = [k for k, _ in STEPS]
        self.stack.setCurrentIndex(0)
        self._next()

    def _mark(self, key: str, state: str, note: str = "") -> None:
        self.state[key] = state
        if note:
            self.notes[key] = note
        for i in range(self.step_list.count()):
            it = self.step_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) == key:
                title = dict(STEPS)[key]
                it.setText(f"{ICON[state]}  {title}" + (f"\n     {note}" if note else ""))
                it.setToolTip(note)

    def _next(self) -> None:
        if not self._queue:
            self._finish()
            return
        key = self._queue.pop(0)
        self._mark(key, "run")
        self._say("")
        self._say(f"── {dict(STEPS)[key]} " + "─" * 20)
        getattr(self, f"_step_{key}")()

    # 1. 설치 — 먼저 '이미 됐나' 를 보고, 필요할 때만 setup.bat, 끝나면 실제 상태로 판정
    def _step_install(self) -> None:
        if not self.venv_exists():
            self._say("가상환경(.venv)이 없습니다. setup.bat 으로 설치합니다.")
            self._run_setup()
            return

        def pre(_rc: int, out: str) -> None:
            st = last_json(out)
            if isinstance(st, dict):
                self.install_state = st
                gaps = setupcheck.missing(st)
                if not gaps:
                    self._mark("install", "ok", "이미 설치됨 — setup.bat 생략")
                    self._next()
                    return
                self._say("설치가 덜 됐습니다: " + " · ".join(gaps))
            else:
                self._say("설치 상태를 읽지 못했습니다. setup.bat 으로 설치합니다.")
            self._run_setup()
        self._run(self.state_command(), pre)

    def _run_setup(self) -> None:
        cmd = self.setup_command()
        if cmd is None:
            self._mark("install", "skip", "Windows 가 아니라 setup.bat 을 건너뜀")
            self._next()
            return

        def done(rc: int, _out: str) -> None:
            self.setup_rc = rc
            self._verify_install()
        self._run(cmd, done)

    def _verify_install(self) -> None:
        """setup.bat 뒤: 종료 코드가 아니라 실제 설치 상태로 판정합니다.
        (setup.bat 은 Playwright 가 실패해도 0 으로 끝나고, 반대로 끝의 pause·
        echo 가 실패해도 0 이 아닐 수 있습니다.)"""
        rc = getattr(self, "setup_rc", 0)
        if not self.venv_exists():
            self._mark("install", "fail", f"가상환경(.venv)이 만들어지지 않음 (setup.bat 종료 코드 {rc})")
            self._next()
            return

        def post(_rc: int, out: str) -> None:
            st = last_json(out)
            if not isinstance(st, dict):
                self._mark("install", "fail", "설치 뒤 상태를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.install_state = st
            gaps = setupcheck.missing(st)
            code = f" (setup.bat 종료 코드 {rc})" if rc else ""
            if not gaps:
                self._mark("install", "ok", "설치 완료" + code)
            elif gaps == ["패키지: " + ", ".join(st.get("reqs_bad", []))]:
                # 패키지만 남음 → 바로 다음 '라이브러리 확인' 단계가 채우도록 만든 흐름.
                self._mark("install", "ok", "설치됨 — 남은 패키지는 다음 단계에서 채움" + code)
            else:
                self._mark("install", "warn", "덜 됨: " + " · ".join(gaps) + code)
            self._next()
        self._run(self.state_command(), post)

    # 2. 라이브러리
    def _step_verify(self, retried: bool = False) -> None:
        def done(_rc: int, out: str) -> None:
            data = last_json(out)
            if not isinstance(data, dict):
                self._mark("verify", "fail", "확인 결과를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.reqs = data.get("items", [])
            bad = [i for i in self.reqs if i["status"] in ("missing", "mismatch")]
            for i in self.reqs:
                self._say(f"  {i['status']:<9} {i['name']:<16} {i.get('detail', '')}")
            if not bad:
                self._mark("verify", "ok", f"{len(self.reqs)}줄 모두 맞음")
                self._next()
            elif retried:
                self._mark("verify", "warn", "설치 뒤에도 안 맞음: "
                           + ", ".join(i["name"] for i in bad))
                self._next()
            else:
                self._say(f"빠졌거나 버전이 안 맞는 {len(bad)}개를 설치합니다.")
                self._run(self.pip_install_command([i["line"] for i in bad]),
                          lambda _rc, _o: self._step_verify(retried=True))
        self._run(self.reqcheck_command(), done)

    # 3. 비밀번호
    def _step_password(self) -> None:
        def done(_rc: int, out: str) -> None:
            data = last_json(out)
            if not isinstance(data, dict):
                self._mark("password", "fail", "자격증명 상태를 읽지 못함 — 출력 보기")
                self._next()
                return
            self.secret_status = data
            where = data.get("eclass", "없음")
            if where != "없음":
                self._mark("password", "ok", f"저장됨({where})")
                self._next()
            elif self._pw_rounds >= 1:
                self._mark("password", "warn", "아직 없음 — 나중에 scripts\\set_password.bat")
                self._next()
            else:
                self._open_password_window()
        self._run(self.status_command(), done)

    def _open_password_window(self) -> None:
        self._pw_rounds += 1
        cmd = self.password_command()
        self._say("eClass 비밀번호가 저장돼 있지 않습니다. 비밀번호 저장 창을 엽니다.")
        self._say(f"> {' '.join(cmd)}")
        self.headline.setText("새로 뜬 검은 창(set_password)에 eClass 비밀번호를 입력하세요. "
                              "입력한 글자는 보이지 않습니다. 그 창을 닫으면 계속합니다.")
        try:
            self.pw_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=self._env(),
                                            creationflags=eclass_fetch.NEW_CONSOLE)
        except OSError as exc:
            self._mark("password", "fail", f"비밀번호 창을 열지 못함: {exc}")
            self._next()
            return
        self._poll.start()

    def _check_password_window(self) -> None:
        if self.pw_proc is None or self.pw_proc.poll() is None:
            return
        self._poll.stop()
        self.pw_proc = None
        self.headline.setText("저장한 설정으로 설치와 점검을 이어서 진행합니다.")
        self._say("비밀번호 창이 닫혔습니다. 다시 확인합니다.")
        self._step_password()

    # 4. 점검
    def _step_check(self) -> None:
        def doctor_done(_rc: int, out: str) -> None:
            self.doctor = last_json(out)
            self._run(self.devices_command(), devices_done)

        def devices_done(_rc: int, out: str) -> None:
            self.devices = last_json(out)
            if not isinstance(self.doctor, dict):
                self._mark("check", "fail", "doctor 결과를 읽지 못함 — 출력 보기")
            elif self.doctor.get("ok"):
                self._mark("check", "ok", "모두 정상")
            else:
                self._mark("check", "warn", self.doctor.get("verdict") or "확인할 항목 있음")
            self._next()
        self._run(self.doctor_command(), doctor_done)

    # -- 끝: 결과 화면 ----------------------------------------------------
    def _finish(self) -> None:
        bad = [k for k, s in self.state.items() if s in ("warn", "fail")]
        self.headline.setText("설치와 점검을 마쳤습니다. " + (
            "아래 ⚠·✖ 항목을 확인하세요." if bad else "모두 정상입니다."))
        self.result_scroll.setWidget(self._build_results())
        self.stack.setCurrentIndex(1)
        self.log_btn.setEnabled(True)
        self.close_btn.setText("닫기")
        self.close_btn.clicked.disconnect()
        self.close_btn.clicked.connect(self.accept)

    def _row(self, grid: QGridLayout, r: int, mark: str, name: str, detail: str) -> None:
        """아이콘 | 굵은 이름 + (아래 줄) 회색 설명. 한 칸에 쌓아 긴 이름이 설명을
        밀어내지 않게 하고, 좁으면 줄바꿈됩니다."""
        icon, color = MARK.get(mark, ("•", "#6B7280"))
        a = QLabel(f"<span style='color:{color}; font-weight:bold'>{icon}</span>")
        a.setAlignment(Qt.AlignmentFlag.AlignTop)
        text = f"<b>{_esc(name)}</b>"
        if detail:
            text += f"<br><span style='color:#4B5563'>{_esc(detail)}</span>"
        b = QLabel(text)
        b.setTextFormat(Qt.TextFormat.RichText)
        b.setWordWrap(True)
        b.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(a, r, 0)
        grid.addWidget(b, r, 1, 1, 2)
    def _group(self, title: str) -> tuple[QGroupBox, QGridLayout]:
        box = QGroupBox(title)
        grid = QGridLayout(box)
        grid.setColumnStretch(2, 1)
        grid.setVerticalSpacing(6)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        return box, grid

    def _config_values(self) -> dict:
        try:
            from .. import settings_schema as S

            return S.current_values(self.config_path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            return {}

    def checklist(self) -> list[tuple[str, str, str]]:
        """setup.bat 이 끝에 안내하던 '다음 순서' 를 (상태, 할 일, 설명) 으로."""
        cfg = self._config_values()
        need = [k for k in ("github.owner", "github.repo", "eclass.username")
                if not str(cfg.get(k, "")).strip()]
        has_tt = bool(cfg.get("recording.timetable"))
        s1 = ("O" if not need and has_tt else "X",
              "1) config.toml 설정 (github.owner / repo, eclass.username, 시간표)",
              "비어 있음: " + ", ".join(need + ([] if has_tt else ["recording.timetable"]))
              if need or not has_tt else "채워져 있음")
        eclass_pw = self.secret_status.get("eclass", "")
        gh = self.secret_status.get("github", "")
        s2 = ("O" if eclass_pw and eclass_pw != "없음" else "X",
              "2) 비밀번호·토큰 저장 (scripts\\set_password.bat)",
              f"eClass: {eclass_pw or '확인 못 함'} · GitHub 토큰: {gh or '확인 못 함'}")
        d = self.doctor if isinstance(self.doctor, dict) else {}
        s3 = ("O" if d.get("ok") else "X", "3) 환경 점검 (scripts\\doctor.bat)",
              d.get("verdict") or "결과 없음")
        dev = self.devices if isinstance(self.devices, dict) else {}
        set_sys = str(cfg.get("recording.audio.system_device", "") or "")
        set_mic = str(cfg.get("recording.audio.mic_device", "") or "")
        s4 = ("O" if dev and not dev.get("audio_error") else "X",
              "4) 오디오 장치 이름 확인 (scripts\\devices.bat)",
              f"시스템 소리: {set_sys or '기본 장치'} · 마이크: {set_mic or '기본 장치'}"
              if dev and not dev.get("audio_error") else (dev.get("audio_error") or "결과 없음"))
        return [s1, s2, s3, s4]

    def _build_results(self) -> QWidget:
        body = QWidget()
        lay = QVBoxLayout(body)

        # 다음 순서 체크 목록
        box, grid = self._group("다음 순서 — 했는지 확인")
        for r, (mark, what, detail) in enumerate(self.checklist()):
            self._row(grid, r, mark, what, detail)
        lay.addWidget(box)

        # 단계 요약
        box, grid = self._group("설치 단계")
        for r, (key, title) in enumerate(STEPS):
            st = self.state[key]
            mark = {"ok": "O", "warn": "!", "fail": "X", "skip": "-"}.get(st, "-")
            self._row(grid, r, mark, title, self.notes.get(key, ""))
        lay.addWidget(box)

        # 라이브러리
        if self.reqs:
            box, grid = self._group("라이브러리 (requirements.txt ↔ 설치됨)")
            for r, i in enumerate(self.reqs):
                mark = {"ok": "O", "skip": "-", "missing": "X", "mismatch": "!"}.get(i["status"], "X")
                need = i.get("spec") or "아무 버전"
                self._row(grid, r, mark, i["name"],
                          f"필요 {need} · 설치 {i.get('installed') or '없음'} · {i.get('detail', '')}")
            lay.addWidget(box)

        # doctor
        d = self.doctor if isinstance(self.doctor, dict) else None
        box, grid = self._group("환경 점검 (doctor.bat)")
        r = 0
        if d is None:
            self._row(grid, r, "X", "doctor", "결과를 읽지 못했습니다(출력 보기)")
        else:
            verdict = QLabel(f"<b style='color:{'#15803D' if d.get('ok') else '#C0392B'}'>"
                             f"{_esc(d.get('verdict', ''))}</b>")
            grid.addWidget(verdict, r, 0, 1, 3)
            r += 1
            for label, value in d.get("info", []):
                self._row(grid, r, "•", label, value)
                r += 1
            if d.get("config_error"):
                self._row(grid, r, "X", "설정 파일", d["config_error"])
                r += 1
            for sec in d.get("sections", []):
                head = QLabel(f"<b>{_esc(sec['title'])}</b>")
                head.setStyleSheet("margin-top: 6px;")
                grid.addWidget(head, r, 0, 1, 3)
                r += 1
                for it in sec.get("items", []):
                    self._row(grid, r, it.get("mark", "-"), it.get("name", ""), it.get("detail", ""))
                    r += 1
        lay.addWidget(box)

        # devices
        dev = self.devices if isinstance(self.devices, dict) else None
        box, grid = self._group("오디오 장치 · 화면 (devices.bat)")
        r = 0
        cfg = self._config_values()
        if dev is None:
            self._row(grid, r, "X", "devices", "결과를 읽지 못했습니다(출력 보기)")
        else:
            if dev.get("audio_error"):
                self._row(grid, r, "X", "오디오", dev["audio_error"])
                r += 1
            for g in dev.get("groups", []):
                key = g.get("config_key", "")
                want = str(cfg.get(key, "") or "") if key else ""
                title = g["title"] + (f"  → {key}" if key else "")
                head = QLabel(f"<b>{_esc(title)}</b>" + (
                    f" <span style='color:#6B7280'>(지금 설정: {_esc(want) or '기본 장치'})</span>"
                    if key else ""))
                head.setStyleSheet("margin-top: 6px;")
                grid.addWidget(head, r, 0, 1, 3)
                r += 1
                if not g.get("devices"):
                    self._row(grid, r, "-", "(없음)", "")
                    r += 1
                for d_ in g.get("devices", []):
                    tags = []
                    if d_.get("default"):
                        tags.append("기본")
                    if want and want.lower() in d_["name"].lower():
                        tags.append("← 지금 설정과 일치")
                    self._row(grid, r, "O" if tags else "•", d_["name"], " · ".join(tags))
                    r += 1
            head = QLabel("<b>화면</b>  <span style='color:#6B7280'>"
                          "→ [recording.video] monitor (0 = 전체 바탕화면)</span>")
            head.setStyleSheet("margin-top: 6px;")
            grid.addWidget(head, r, 0, 1, 3)
            r += 1
            for m in dev.get("monitors", []):
                self._row(grid, r, "•", f"monitor = {m['index']}", m.get("text", ""))
                r += 1
            hint = QLabel("config.toml 에는 장치 이름의 일부만 적어도 됩니다(부분 일치). "
                          "파일 → 설정 편집 에서 바꿀 수 있습니다.")
            hint.setStyleSheet("color: #6B7280;")
            hint.setWordWrap(True)
            grid.addWidget(hint, r, 0, 1, 3)
        lay.addWidget(box)
        lay.addStretch(1)
        return body

    # -- 정리 --------------------------------------------------------------
    def done(self, r: int) -> None:
        self._poll.stop()
        p, self.proc = self.proc, None
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            try:
                p.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            eclass_fetch._kill_tree(int(p.processId()))
            p.kill()
            p.waitForFinished(3000)
        if self.pw_proc is not None and self.pw_proc.poll() is None:
            eclass_fetch._kill_tree(self.pw_proc.pid)
        super().done(r)

    def exec(self) -> int:
        QTimer.singleShot(0, self.start)
        return super().exec()
