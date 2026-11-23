"""soundcard 가 열지 못하는 마이크를 ffmpeg(DirectShow)로 여는 대체 경로.

2026-11-23 강의실 PC(HUM-2F-1121)의 메인보드 마이크 단자 '마이크(High Definition Audio
Device)' 는 Windows 공유 모드 형식(mix format)이 IEEE_FLOAT(0x0003)·32비트·48kHz·2채널,
포장 없음(cbSize 0)이었습니다. soundcard 0.4.6 은 이 형식을 WAVE_FORMAT_EXTENSIBLE(0xFFFE)
으로만 받아들여 `assert ... wFormatTag == 0xFFFE` 에서 바로 죽습니다(mediafoundation.py:516).
실제 소리 형식(float32)은 soundcard 가 원하는 것과 같은데 겉 포장 때문에 거절하는 것입니다.
같은 장치를 ffmpeg -f dshow 로는 문제없이 엽니다. 그래서 soundcard 가 이렇게 실패하면
같은 이름의 DirectShow 장치를 열어 soundcard 마이크와 같은 모양(recorder()/record())으로
넘깁니다 — 녹음 스레드·무음 감지·실시간 전사·VAD 는 그대로입니다.

DirectShow 는 녹음 장치만 다룹니다. 시스템 소리(루프백)에는 쓰지 않습니다.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache

from ..logging_setup import get as get_logger

log = get_logger("recorder.dshow_mic")

FORMAT_ERROR = ("soundcard 가 이 장치의 형식을 받지 못했습니다(Windows 공유 모드 형식이 "
                "WAVE_FORMAT_EXTENSIBLE 이 아님 — 메인보드 마이크 단자 드라이버에서 흔함)")
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_format_error(exc: BaseException) -> bool:
    """soundcard 가 장치 형식을 단언(assert)하다 실패한 것인가."""
    if not isinstance(exc, AssertionError):
        return False
    tb = exc.__traceback__
    while tb is not None:
        if "soundcard" in tb.tb_frame.f_code.co_filename.replace("\\", "/"):
            return True
        tb = tb.tb_next
    return False


# ---------------------------------------------------------------------------
# 장치 찾기
# ---------------------------------------------------------------------------
def _ffmpeg() -> str | None:
    from .ffmpeg_util import FFmpegMissing, find_ffmpeg

    try:
        return find_ffmpeg()
    except FFmpegMissing:
        return None


@lru_cache(maxsize=4)
def dshow_audio_devices(ffmpeg: str) -> tuple[str, ...]:
    """ffmpeg 가 보는 DirectShow 녹음 장치 이름들."""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow",
                            "-i", "dummy"], capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("DirectShow 장치 목록을 읽지 못했습니다: %s", exc)
        return ()
    names = []
    for line in r.stderr.decode("utf-8", "replace").splitlines():
        if line.rstrip().endswith("(audio)") and '"' in line:
            names.append(line.split('"')[1])
    return tuple(names)


def match_name(want: str, names: tuple[str, ...] | list[str]) -> str | None:
    """soundcard 이름과 같은 DirectShow 장치. 같은 이름이 먼저, 없으면 한쪽이 다른 쪽을 품는 것."""
    if want in names:
        return want
    low = want.lower()
    for n in names:
        if n.lower() == low:
            return n
    for n in names:
        if n and (n.lower() in low or low in n.lower()):
            return n
    return None


class DshowMic:
    """soundcard 마이크와 같은 모양의 ffmpeg DirectShow 마이크."""

    isloopback = False

    def __init__(self, name: str, ffmpeg: str, *, popen: Callable = subprocess.Popen) -> None:
        self.name = name
        self.id = f"dshow:{name}"
        self.ffmpeg = ffmpeg
        self._popen = popen

    def __repr__(self) -> str:
        return f"<DshowMic {self.name}>"

    def recorder(self, samplerate: int = 48000, channels=None, blocksize=None):
        return _DshowRecorder(self, int(samplerate), int(channels or 2))


class _DshowRecorder:
    def __init__(self, mic: DshowMic, samplerate: int, channels: int) -> None:
        self.mic, self.samplerate, self.channels = mic, samplerate, channels
        self.proc = None
        self._err: list[str] = []

    def __enter__(self):
        cmd = [self.mic.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={self.mic.name}",
               "-ac", str(self.channels), "-ar", str(self.samplerate), "-f", "f32le", "-"]
        self.proc = self.mic._popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        threading.Thread(target=self._drain, daemon=True).start()
        return self

    def _drain(self) -> None:
        for raw in iter(self.proc.stderr.readline, b""):
            self._err = (self._err + [raw.decode("utf-8", "replace").strip()])[-5:]

    def record(self, numframes: int = 4096):
        import numpy as np

        want = int(numframes) * self.channels * 4
        buf = bytearray()
        while len(buf) < want:
            chunk = self.proc.stdout.read(want - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        if not buf:
            code = self.proc.poll()
            why = "; ".join(x for x in self._err if x) or f"종료 코드 {code}"
            raise RuntimeError(f"ffmpeg(DirectShow)로 '{self.mic.name}' 녹음이 끊겼습니다: {why}")
        usable = len(buf) - len(buf) % (self.channels * 4)
        return np.frombuffer(bytes(buf[:usable]), dtype="<f4").reshape(-1, self.channels)

    def __exit__(self, *_exc) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def alternative(source, *, ffmpeg: str | None = None,
                devices: Callable[[str], tuple[str, ...]] = dshow_audio_devices,
                platform: str = sys.platform) -> DshowMic | None:
    """source(soundcard 마이크)와 같은 장치를 ffmpeg DirectShow 로. 못 하면 None."""
    if platform != "win32" or getattr(source, "isloopback", False):
        return None
    if isinstance(source, DshowMic):
        return source
    ff = ffmpeg or _ffmpeg()
    if not ff:
        return None
    name = match_name(str(getattr(source, "name", "") or ""), devices(ff))
    return DshowMic(name, ff) if name else None


@contextmanager
def open_recording(source, *, samplerate: int, channels, blocksize=None,
                   prefer_dshow: bool = False,
                   on_fallback: Callable[[DshowMic, str], None] | None = None,
                   alt: Callable | None = None) -> Iterator:
    """source 로 녹음을 엽니다. soundcard 가 형식 문제로 못 열면 같은 장치를 ffmpeg 로.

    prefer_dshow(recording.audio.backend = "dshow")면 처음부터 ffmpeg 로 엽니다(녹음 장치만).
    열지 못하면 원인이 적힌 RuntimeError — 빈 AssertionError 를 그대로 올리지 않습니다
    (로그에 원인이 안 남아 '신호 0' 으로 오인됐음).
    """
    alt = alt or alternative            # 부를 때 찾음(바꿔 끼울 수 있게)
    cm = None
    if prefer_dshow:
        d = alt(source)
        if d is not None:
            cm = d.recorder(samplerate=samplerate, channels=channels, blocksize=blocksize)
            rec = cm.__enter__()
            if on_fallback:
                on_fallback(d, "설정(backend = dshow)")
    if cm is None:
        try:
            cm = source.recorder(samplerate=samplerate, channels=channels, blocksize=blocksize)
            rec = cm.__enter__()
        except Exception as exc:
            if not is_format_error(exc):
                raise
            d = alt(source)
            if d is None:
                raise RuntimeError(FORMAT_ERROR + ". ffmpeg 가 있으면 같은 장치를 대신 열 수 있는데, "
                                   "ffmpeg 나 같은 이름의 DirectShow 장치를 찾지 못했습니다.") from exc
            cm = d.recorder(samplerate=samplerate, channels=channels, blocksize=blocksize)
            rec = cm.__enter__()
            if on_fallback:
                on_fallback(d, FORMAT_ERROR)
    try:
        yield rec
    finally:
        cm.__exit__(None, None, None)
