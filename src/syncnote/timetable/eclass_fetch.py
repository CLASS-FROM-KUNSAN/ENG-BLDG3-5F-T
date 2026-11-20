"""eClass 과목 목록 받기: eclass_probe.bat → (비밀번호 없으면) set_password.bat 1 → 다시.

편집 → 'eClass 과목과 연결…' 을 눌렀는데 이 PC 에 과목 목록이 없을 때 씁니다.

1. scripts/eclass_probe.bat 을 실행하고 출력을 이 창에 보여 줍니다(브라우저 창이
   뜹니다). probe 는 과목 목록을 state/eclass_courses.json 에 남깁니다.
2. 출력에 비밀번호를 저장하라는 명령(`secrets set eclass`)이 나오면 probe 를
   (브라우저까지) 닫고, scripts/set_password.bat 을 `1`(Eclass 비밀번호) 로 새 콘솔
   창에 엽니다. 비밀번호 입력은 화면에 보이지 않아야 해서 진짜 콘솔이 필요합니다.
3. 그 창이 닫히면 probe 를 한 번 더 돌립니다. 그래도 비밀번호를 달라고 하면
   멈추고 알립니다(무한 반복 방지).
4. '과목 목록 N개를 남겼습니다' 가 나오면 성공으로 닫습니다.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"

# probe 가 비밀번호를 달라고 할 때 나오는 명령(없음·틀림 둘 다 이 문구를 씀).
PASSWORD_HINT = re.compile(r"secrets\s+set\s+eclass")
# probe 가 과목 목록을 남겼을 때(eclass/probe.py 의 print 와 짝).
SAVED_HINT = re.compile(r"과목 목록 (\d+)개를 남겼습니다")
MAX_PASSWORD_ROUNDS = 1
# 비밀번호 입력은 화면에 안 보여야 해서 진짜 콘솔 창이 필요합니다(테스트는 0 으로 바꿈).
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def _kill_tree(pid: int) -> None:
    """bat → python → 브라우저까지 통째로 닫기."""
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
    else:  # pragma: no cover
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


class CourseListFetcher(QDialog):
    def __init__(self, parent: QWidget | None, config_path: Path) -> None:
        super().__init__(parent)
        from .gui import ScreenKeeper

        self.config_path = Path(config_path)
        self.setWindowTitle("eClass 과목 목록 받기")
        self.resize(640, 420)
        ScreenKeeper.attach(self)
        lay = QVBoxLayout(self)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)
        bb = QDialogButtonBox()
        self.stop_btn = QPushButton("중단")
        self.stop_btn.clicked.connect(self.reject)
        bb.addButton(self.stop_btn, QDialogButtonBox.ButtonRole.RejectRole)
        lay.addWidget(bb)

        self.proc: QProcess | None = None
        self.pw_proc: subprocess.Popen | None = None
        self.password_rounds = 0
        self.saved = 0
        self.needs_password = False
        self._buf = ""
        self._poll = QTimer(self)
        self._poll.setInterval(400)
        self._poll.timeout.connect(self._check_password_window)

    # -- 실행할 명령(테스트에서 바꿔 끼움) -------------------------------
    def probe_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "eclass_probe.bat")]
        return [sys.executable, "-m", "syncnote", "eclass", "probe", "--no-headless"]

    def password_command(self) -> list[str]:
        if os.name == "nt":
            return ["cmd.exe", "/d", "/c", str(SCRIPTS / "set_password.bat"), "1"]
        return [sys.executable, "-m", "syncnote", "secrets", "set", "eclass"]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["SYNCNOTE_CONFIG"] = str(self.config_path)   # 편집기가 연 설정으로
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    # -- 1. probe ---------------------------------------------------------
    def start(self) -> None:
        self.saved = 0
        self.needs_password = False
        self._buf = ""
        self.status.setText("eClass 에 로그인해 과목 목록을 가져옵니다 (scripts\\eclass_probe.bat). "
                            "브라우저 창이 뜹니다. 끝나면 이 창이 저절로 닫힙니다.")
        cmd = self.probe_command()
        self._say(f"> {' '.join(cmd)}")
        p = QProcess(self)
        p.setWorkingDirectory(str(REPO_ROOT))
        env = QProcessEnvironment()
        for k, v in self._env().items():
            env.insert(k, v)
        p.setProcessEnvironment(env)
        p.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        p.readyReadStandardOutput.connect(self._read)
        p.finished.connect(self._probe_finished)
        p.errorOccurred.connect(self._probe_error)
        self.proc = p
        p.start(cmd[0], cmd[1:])
        # bat 끝의 pause 가 키를 기다리지 않게 입력을 닫아 둡니다.
        p.closeWriteChannel()

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text.rstrip("\n"))

    def _read(self) -> None:
        if self.proc is None:
            return
        chunk = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._buf += chunk
        for line in chunk.replace("\r\n", "\n").splitlines():
            if line.strip():
                self._say(line)
        m = SAVED_HINT.search(self._buf)
        if m:
            self.saved = int(m.group(1))
        if not self.needs_password and PASSWORD_HINT.search(self._buf):
            self.needs_password = True
            QTimer.singleShot(0, self._password_needed)

    def _probe_error(self, err) -> None:
        if err == QProcess.ProcessError.FailedToStart:
            self._say(f"! 실행하지 못했습니다: {self.proc.errorString() if self.proc else err}")
            self._fail("eclass_probe 를 실행하지 못했습니다.")

    def _probe_finished(self, *_a) -> None:
        if self.needs_password:
            return                     # 비밀번호 단계가 이어받음
        if self.saved:
            self._say(f"✔ 과목 {self.saved}개를 받았습니다.")
            self.accept()
            return
        self._fail("과목 목록을 받지 못했습니다. 위 출력을 확인하세요 "
                   "(학번 설정·학교망·사이트 구조 변경 등).")

    def _stop_probe(self) -> None:
        p, self.proc = self.proc, None
        if p is not None and p.state() != QProcess.ProcessState.NotRunning:
            try:
                p.finished.disconnect(self._probe_finished)
            except (RuntimeError, TypeError):
                pass
            _kill_tree(int(p.processId()))
            p.kill()
            p.waitForFinished(3000)

    # -- 2. 비밀번호 --------------------------------------------------------
    def _password_needed(self) -> None:
        self._stop_probe()
        if self.password_rounds >= MAX_PASSWORD_ROUNDS:
            self._fail("비밀번호를 저장했는데도 다시 비밀번호를 요구합니다. 학번(eclass.username)과 "
                       "비밀번호를 확인하세요. 계정이 잠겼을 수도 있습니다.")
            return
        self.password_rounds += 1
        cmd = self.password_command()
        self._say("")
        self._say("비밀번호가 필요합니다. eclass_probe 를 닫고 비밀번호 저장 창을 엽니다.")
        self._say(f"> {' '.join(cmd)}")
        self.status.setText("새로 뜬 검은 창(set_password)에 eClass 비밀번호를 입력하세요. "
                            "입력한 글자는 보이지 않습니다. 그 창을 닫으면 과목 목록을 다시 가져옵니다.")
        flags = NEW_CONSOLE
        try:
            self.pw_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=self._env(),
                                            creationflags=flags)
        except OSError as exc:
            self._fail(f"비밀번호 저장 창을 열지 못했습니다: {exc}")
            return
        self._poll.start()

    def _check_password_window(self) -> None:
        if self.pw_proc is None or self.pw_proc.poll() is None:
            return
        self._poll.stop()
        self.pw_proc = None
        self._say("비밀번호 창이 닫혔습니다. 과목 목록을 다시 가져옵니다.")
        self._say("")
        self.start()

    # -- 끝 ----------------------------------------------------------------
    def _fail(self, why: str) -> None:
        self.status.setText(f"⚠ {why}")
        self.stop_btn.setText("닫기")

    def done(self, r: int) -> None:   # 닫히면 남은 프로세스도 정리
        self._poll.stop()
        self._stop_probe()
        if self.pw_proc is not None and self.pw_proc.poll() is None:
            _kill_tree(self.pw_proc.pid)
        super().done(r)

    def exec(self) -> int:
        QTimer.singleShot(0, self.start)
        return super().exec()
