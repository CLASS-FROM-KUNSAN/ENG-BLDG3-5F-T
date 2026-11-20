"""ffmpeg 찾기와 공통 실행 유틸.

ffmpeg 를 쓰는 이유:
  순수 파이썬(mss + opencv + sounddevice)으로 장시간 녹화를 하면
  프레임 누락이 누적돼 영상과 소리가 점점 어긋납니다. 1시간이면
  수십 초까지 벌어집니다. ffmpeg 는 이 문제를 이미 풀어 놓았습니다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.ffmpeg")


class FFmpegMissing(Exception):
    def __str__(self) -> str:
        return (
            "ffmpeg 를 찾지 못했습니다.\n"
            "  1) https://www.gyan.dev/ffmpeg/builds/ 에서 release-essentials 를 받고\n"
            "  2) 압축을 푼 뒤 bin 폴더를 PATH 에 추가하거나\n"
            "  3) 환경변수 FFMPEG 에 ffmpeg.exe 전체 경로를 넣으세요.\n"
            "  (scripts/setup.bat 이 자동으로 받아 줍니다)"
        )


def find_ffmpeg() -> str:
    for candidate in (os.environ.get("FFMPEG"), "ffmpeg", "ffmpeg.exe"):
        if not candidate:
            continue
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    # 레포 동봉 위치도 봅니다
    here = Path(__file__).resolve().parents[3]
    for rel in ("tools/ffmpeg/bin/ffmpeg.exe", "tools/ffmpeg/ffmpeg.exe", "tools/ffmpeg"):
        p = here / rel
        if p.exists():
            return str(p)
    raise FFmpegMissing()


def find_ffprobe() -> str | None:
    for candidate in (os.environ.get("FFPROBE"), "ffprobe", "ffprobe.exe"):
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    try:
        ff = Path(find_ffmpeg())
        probe = ff.with_name("ffprobe" + ff.suffix)
        return str(probe) if probe.exists() else None
    except FFmpegMissing:
        return None


def _creationflags() -> int:
    """Windows 에서 콘솔 창이 깜빡이지 않게 합니다."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def run(args: list[str], *, timeout: float | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("실행: %s", " ".join(args))
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=_creationflags(),
    )
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg 실패 (code {proc.returncode}):\n{tail}")
    return proc


def popen(args: list[str], *, stdin_pipe: bool = True,
          stderr_log: Path | None = None) -> subprocess.Popen:
    """오래 도는 ffmpeg 를 띄웁니다.

    **stderr 를 파이프로 받지 않습니다.** ffmpeg 는 -loglevel warning
    에서도 녹화 중에 계속 stderr 에 씁니다(프레임 드롭, 인코더 경고).
    아무도 읽지 않는 파이프는 버퍼가 차는 순간 ffmpeg 의 write 를
    막아 세웁니다. 윈도우 파이프 버퍼는 4KB 라 긴 녹화에서는 쉽게
    찹니다. 그러면 그 뒤 프레임이 파일에 들어가지 않는데 poll() 은
    None 이라 '녹화 중' 으로 보입니다. 사용자는 끝까지 찍힌 줄 알고
    있다가 토막 난 파일을 받습니다.

    진단이 필요하면 stderr_log 에 파일 경로를 주세요. 파일은 버퍼가
    차지 않습니다. 주지 않으면 버립니다.
    """
    log.debug("백그라운드 실행: %s", " ".join(args))
    if stderr_log is not None:
        Path(stderr_log).parent.mkdir(parents=True, exist_ok=True)
        # 핸들을 닫지 않습니다 — ffmpeg 가 살아 있는 동안 열려 있어야 합니다.
        err = Path(stderr_log).open("ab", buffering=0)  # noqa: SIM115
    else:
        err = subprocess.DEVNULL
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=err,
        creationflags=_creationflags(),
    )
    # 파일 핸들을 프로세스에 묶어 둡니다. 여기서 닫으면 ffmpeg 가
    # 끝나기 전에 파일이 사라집니다.
    proc._syncnote_stderr = err if stderr_log is not None else None  # type: ignore[attr-defined]
    return proc


def read_stderr_log(path: Path | None, lines: int = 10) -> str:
    """ffmpeg 가 남긴 로그의 끝부분. 없으면 빈 문자열."""
    if path is None:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def duration_of(path: Path) -> float | None:
    """미디어 길이(초). ffprobe 가 없으면 None."""
    probe = find_ffprobe()
    if not probe:
        return None
    try:
        proc = run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            timeout=30,
        )
        return float((proc.stdout or "").strip())
    except Exception:
        return None
