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


def popen(args: list[str], *, stdin_pipe: bool = True) -> subprocess.Popen:
    log.debug("백그라운드 실행: %s", " ".join(args))
    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_creationflags(),
    )


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
