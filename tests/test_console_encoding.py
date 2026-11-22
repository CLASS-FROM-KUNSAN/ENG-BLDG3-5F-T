"""콘솔·로그 파일 인코딩이 UTF 가 아니어도 CLI 가 출력 때문에 죽지 않는지.

영문 Windows(cp1252) 콘솔, 한국어 Windows 에서 출력을 파일로 돌릴 때(cp949 — 한글은
되지만 '—' 에서 죽음)를 재현합니다. 리눅스 CI 에서도 PYTHONIOENCODING 으로 같은 상황을
만듭니다.
"""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from syncnote import cli

ROOT = Path(__file__).resolve().parents[1]


def _env(**extra):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "0"}
    env.pop("PYTHONIOENCODING", None)
    env.update(extra)
    return env


@pytest.mark.parametrize("enc", ["cp1252", "cp949", "ascii"])
def test_help_survives_a_non_utf_console(enc):
    p = subprocess.run([sys.executable, "-m", "syncnote", "--help"], capture_output=True,
                       env=_env(PYTHONIOENCODING=enc), timeout=120)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")[-400:]
    assert b"usage: syncnote" in p.stdout


class _Stream(io.TextIOWrapper):
    def __init__(self, enc, tty):
        super().__init__(io.BytesIO(), encoding=enc)
        self._tty = tty

    def isatty(self):
        return self._tty


def test_redirected_output_becomes_utf8_unless_the_user_chose_an_encoding(monkeypatch):
    out, err = _Stream("cp949", tty=False), _Stream("cp1252", tty=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    cli._safe_console()
    assert out.encoding == "utf-8"                                   # 파일로 돌림 -> UTF-8
    assert err.encoding == "cp1252" and err.errors == "replace"      # 진짜 콘솔 -> ? 로
    print("자료구조 — 녹화", file=err)                               # 죽지 않음

    chosen = _Stream("cp949", tty=False)
    monkeypatch.setattr(sys, "stdout", chosen)
    monkeypatch.setenv("PYTHONIOENCODING", "cp949")
    cli._safe_console()
    assert chosen.encoding == "cp949" and chosen.errors == "replace"  # 사용자가 정한 것은 지킴


def test_utf_streams_are_left_alone(monkeypatch):
    s = _Stream("utf-8", tty=False)
    monkeypatch.setattr(sys, "stdout", s)
    cli._safe_console()
    assert s.encoding == "utf-8" and s.errors == "strict"
