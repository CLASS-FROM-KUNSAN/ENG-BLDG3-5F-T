"""녹음 장치를 여는 '방법' 의 사슬과, 녹음 중 끊겼을 때의 복구(정규화 롤백).

배경(2026-11-23 강의실 PC HUM-2F-1121): 메인보드 마이크 단자 '마이크(High Definition Audio
Device)' 의 공유 모드 형식이 IEEE_FLOAT·포장 없음(cbSize 0)이라 soundcard 0.4.6 이
`assert wFormatTag == 0xFFFE`(mediafoundation.py:516)에서 죽었습니다. 같은 장치를 ffmpeg
DirectShow 로는 엽니다. 그런데 ffmpeg 도 실패할 수 있습니다(장치를 다른 앱이 독점, ffmpeg
없음·고장, 드라이버가 DirectShow 에 안 보임). 그래서 한 방법에 기대지 않고 사슬로 엽니다.

    마이크:      soundcard → ffmpeg → SoX → arecord(리눅스)   (backend = "dshow" 면 ffmpeg 먼저)
    시스템 소리:  soundcard(WASAPI 루프백)만 — 다른 도구는 Windows 루프백을 못 받음

정규화 롤백 — 녹음 중 지금 방법이 끊기면(장치 뽑힘·프로세스 종료):
  1. 지금 방법을 닫고 사슬의 다음 방법으로 엽니다(끝까지 가면 처음부터).
  2. 끊긴 동안의 시간만큼 **무음을 채워** WAV 의 시간축을 지킵니다 — 화면·다른 트랙과
     소리가 어긋나지 않습니다(조각마다 따로 맞추지 않아도 됨).
  3. 전부 실패하면 무음으로 시간을 채우며 2 → 5 → 10 → 30초 간격으로 처음부터 다시 엽니다
     (USB 마이크를 다시 꽂으면 그 순간부터 다시 녹음).
  모든 전환은 기록(events)과 로그에 남습니다.

DirectShow·SoX·arecord 는 모두 ffmpeg/sox/arecord 프로세스의 표준출력으로 float32 PCM 을
받아 soundcard 마이크와 같은 모양(recorder()/record())으로 넘깁니다 — 녹음 스레드·무음
감지·실시간 전사·VAD 는 그대로입니다.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache

from ..logging_setup import get as get_logger

log = get_logger("recorder.dshow_mic")

FORMAT_ERROR = ("soundcard 가 이 장치의 형식을 받지 못했습니다(Windows 공유 모드 형식이 "
                "WAVE_FORMAT_EXTENSIBLE 이 아님 — 메인보드 마이크 단자 드라이버에서 흔함)")
BACKOFF_SEC = (2.0, 5.0, 10.0, 30.0)
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
WINMM_NAME_MAX = 31          # SoX waveaudio(WinMM) 장치 이름은 31자에서 잘립니다


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


def _why(exc: BaseException) -> str:
    if is_format_error(exc):
        return FORMAT_ERROR
    return str(exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# 도구 · 장치 찾기
# ---------------------------------------------------------------------------
def _ffmpeg() -> str | None:
    from .ffmpeg_util import FFmpegMissing, find_ffmpeg

    try:
        return find_ffmpeg()
    except FFmpegMissing:
        return None


def _tool(name: str) -> str | None:
    if name == "ffmpeg":
        return _ffmpeg()
    from .terminal import find_tool

    return find_tool(name)


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
    """soundcard 이름과 같은 장치. 같은 이름이 먼저, 없으면 한쪽이 다른 쪽을 품는 것.

    이름이 비어 있으면 아무것도 고르지 않습니다 — 빈 글자는 모든 이름에 '포함' 되어
    엉뚱한(첫 번째) 장치를 열게 됩니다.
    """
    if not want.strip():
        return None
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


# ---------------------------------------------------------------------------
# 프로세스 표준출력으로 받는 마이크(ffmpeg · SoX · arecord)
# ---------------------------------------------------------------------------
class PipeMic:
    """soundcard 마이크와 같은 모양. build(samplerate, channels) 가 float32 LE 를 내는 명령."""

    isloopback = False
    tool = "pipe"

    def __init__(self, name: str, build: Callable[[int, int], list[str]], *,
                 popen: Callable = subprocess.Popen) -> None:
        self.name = name
        self.id = f"{self.tool}:{name}"
        self._build = build
        self._popen = popen

    @property
    def label(self) -> str:
        return {"ffmpeg": "ffmpeg DirectShow" if sys.platform == "win32" else "ffmpeg",
                "sox": "SoX", "arecord": "arecord"}.get(self.tool, self.tool)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name}>"

    def recorder(self, samplerate: int = 48000, channels=None, blocksize=None):
        ch = int(channels or 2)
        return _PipeRecorder(self, self._build(int(samplerate), ch), ch)


class DshowMic(PipeMic):
    """ffmpeg 로 여는 마이크(Windows 는 DirectShow, 리눅스는 PulseAudio)."""

    tool = "ffmpeg"

    def __init__(self, name: str, ffmpeg: str, *, popen: Callable = subprocess.Popen,
                 fmt: str = "dshow", device: str | None = None) -> None:
        self.ffmpeg = ffmpeg
        dev = f"audio={name}" if fmt == "dshow" else (device or "default")
        extra = ["-audio_buffer_size", "50"] if fmt == "dshow" else []

        def build(sr: int, ch: int) -> list[str]:
            return [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", fmt, *extra,
                    "-i", dev, "-ac", str(ch), "-ar", str(sr), "-f", "f32le", "-"]
        super().__init__(name, build, popen=popen)


class SoxMic(PipeMic):
    tool = "sox"

    def __init__(self, name: str, sox: str, *, popen: Callable = subprocess.Popen,
                 platform: str = sys.platform, device: str | None = None) -> None:
        if platform == "win32":
            src = ["-t", "waveaudio", (device or name)[:WINMM_NAME_MAX]]
        else:
            src = ["-t", "pulseaudio", device or "default"]

        def build(sr: int, ch: int) -> list[str]:
            return [sox, "-q", *src, "-t", "raw", "-e", "floating-point", "-b", "32", "-L",
                    "-c", str(ch), "-r", str(sr), "-"]
        super().__init__(name, build, popen=popen)


class ArecordMic(PipeMic):
    tool = "arecord"

    def __init__(self, name: str, arecord: str, *, popen: Callable = subprocess.Popen,
                 device: str = "default") -> None:
        def build(sr: int, ch: int) -> list[str]:
            return [arecord, "-q", "-t", "raw", "-f", "FLOAT_LE", "-r", str(sr), "-c", str(ch),
                    "-D", device, "-"]
        super().__init__(name, build, popen=popen)


class _PipeRecorder:
    def __init__(self, mic: PipeMic, cmd: list[str], channels: int) -> None:
        self.mic, self.cmd, self.channels = mic, cmd, channels
        self.proc = None
        self._err: list[str] = []

    def __enter__(self):
        self.proc = self.mic._popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
            raise RuntimeError(f"{self.mic.label} 로 '{self.mic.name}' 녹음이 끊겼습니다: {why}")
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


# ---------------------------------------------------------------------------
# 사슬 만들기
# ---------------------------------------------------------------------------
def alternative(source, *, ffmpeg: str | None = None,
                devices: Callable[[str], tuple[str, ...]] = dshow_audio_devices,
                platform: str = sys.platform) -> DshowMic | None:
    """source(soundcard 마이크)와 같은 장치를 ffmpeg 로. 못 하면 None."""
    if getattr(source, "isloopback", False):
        return None
    if isinstance(source, DshowMic):
        return source
    if platform not in ("win32", "linux"):
        return None
    ff = ffmpeg or _ffmpeg()
    if not ff:
        return None
    if platform == "linux":
        return DshowMic(str(getattr(source, "name", "default")), ff, fmt="pulse",
                        device=str(getattr(source, "id", "") or "default"))
    name = match_name(str(getattr(source, "name", "") or ""), devices(ff))
    return DshowMic(name, ff) if name else None


def alternatives(source, *, platform: str = sys.platform,
                 tool: Callable[[str], str | None] = _tool,
                 ffmpeg_alt: Callable | None = None) -> list[PipeMic]:
    """soundcard 다음에 시도할 방법들(있는 도구만). 루프백이면 없음."""
    if getattr(source, "isloopback", False) or isinstance(source, PipeMic):
        return []
    name = str(getattr(source, "name", "") or "default")
    out: list[PipeMic] = []
    ff = (ffmpeg_alt or alternative)(source)
    if ff is not None:
        out.append(ff)
    sox = tool("sox")
    if sox and platform in ("win32", "linux", "darwin"):
        out.append(SoxMic(name, sox, platform=platform,
                          device=None if platform == "win32" else str(getattr(source, "id", "") or "")
                          or None))
    if platform == "linux":
        ar = tool("arecord")
        if ar:
            out.append(ArecordMic(name, ar))
    return out


# ---------------------------------------------------------------------------
# 열기 · 복구
# ---------------------------------------------------------------------------
class _Candidate:
    def __init__(self, label: str, dev) -> None:
        self.label, self.dev = label, dev


class ResilientRecorder:
    """사슬로 열고, 녹음 중 끊기면 다음 방법으로 바꾸며 끊긴 시간만큼 무음을 채웁니다."""

    def __init__(self, cands: list[_Candidate], *, samplerate: int, channels, blocksize=None,
                 on_switch: Callable | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 backoff: tuple[float, ...] = BACKOFF_SEC) -> None:
        self.cands = cands
        self.samplerate, self.blocksize = int(samplerate), blocksize
        self.channels = int(channels or 2)
        self.on_switch = on_switch
        self.clock, self.sleep, self.backoff = clock, sleep, backoff
        self.cm = None
        self.rec = None
        self.index = -1                 # 지금 쓰는 방법
        self.events: list[str] = []
        self.errors: list[str] = []     # 마지막으로 모두 실패했을 때의 이유들
        self.gap_frames = 0             # 아직 채우지 않은 무음
        self.filled_sec = 0.0           # 지금까지 무음으로 채운 시간
        self.last_data = 0.0
        # 시간축: WAV 가 '언제까지' 채워졌나(벽시계). 끊긴 뒤 새 방법의 첫 소리가 오면
        # 그 사이(새 장치가 열리는 1~2초 포함)를 무음으로 채웁니다.
        self.covered = 0.0
        self.await_first = False
        self.pending = None
        self.fails = 0
        self.next_try = 0.0

    # -- 열기 -----------------------------------------------------------
    def _open(self, start: int = 0) -> bool:
        self.errors = []
        n = len(self.cands)
        for k in range(n):
            i = (start + k) % n
            c = self.cands[i]
            try:
                cm = c.dev.recorder(samplerate=self.samplerate, channels=self.channels,
                                    blocksize=self.blocksize)
                rec = cm.__enter__()
            except Exception as exc:
                self.errors.append(f"{c.label}: {_why(exc)}")
                continue
            self.cm, self.rec, self.index = cm, rec, i
            if i != 0 or self.events:
                why = "; ".join(self.errors) or "앞 방법이 끊김"
                self.events.append(f"{c.label} 로 엶 ({why})")
                if self.on_switch:
                    self.on_switch(c.dev, why)
            return True
        return False

    def begin(self) -> ResilientRecorder:
        if not self._open(0):
            raise RuntimeError("녹음 장치를 어느 방법으로도 열지 못했습니다 — " + " / ".join(self.errors))
        self.last_data = self.covered = self.clock()
        # 처음 열 때도 같은 셈: ffmpeg 는 연 뒤 첫 소리까지 ~0.5초 걸립니다(실측). 녹음 트랙은
        # 연 순간을 시작 시각으로 삼아 화면과 맞추므로, 그 사이를 무음으로 채워야 어긋나지 않음.
        self.await_first = True
        return self

    def _close(self) -> None:
        if self.cm is not None:
            try:
                self.cm.__exit__(None, None, None)
            except Exception:
                pass
        self.cm = self.rec = None

    # -- 읽기 -----------------------------------------------------------
    def _silence(self, frames: int):
        import numpy as np

        frames = max(1, int(frames))
        self.filled_sec += frames / self.samplerate
        return np.zeros((frames, self.channels), dtype="float32")

    def record(self, numframes: int = 4096):
        if self.gap_frames > 0:                    # 끊긴 시간만큼 먼저 무음
            n = min(int(numframes), self.gap_frames)
            self.gap_frames -= n
            return self._silence(n)
        if self.pending is not None:               # 무음 다음에 새 방법의 첫 소리
            data, self.pending = self.pending, None
            return data
        if self.rec is None:
            now = self.clock()
            if now >= self.next_try:
                start = (self.index + 1) % len(self.cands) if self.index >= 0 else 0
                if self._open(start):
                    self.fails = 0
                    self.await_first = True
                    return self.record(numframes)
                wait = self.backoff[min(self.fails, len(self.backoff) - 1)]
                self.fails += 1
                self.next_try = now + wait
                self.events.append(f"모든 방법 실패 — {wait:.0f}초 뒤 다시: {' / '.join(self.errors)}")
                log.error("녹음 장치를 어느 방법으로도 열지 못했습니다(%.0f초 뒤 다시, 그동안 무음): %s",
                          wait, " / ".join(self.errors))
            # 기다리는 동안도 실제 시간만큼 무음을 내 시간축을 지킴
            self.sleep(int(numframes) / self.samplerate)
            self.covered += int(numframes) / self.samplerate
            return self._silence(numframes)
        try:
            data = self.rec.record(numframes=numframes)
        except Exception as exc:
            label = self.cands[self.index].label
            self.events.append(f"{label} 끊김: {_why(exc)}")
            log.warning("녹음 중 %s 가 끊겼습니다(%s) — 다음 방법으로 바꿉니다.", label, _why(exc))
            self._close()
            self.next_try = 0.0
            return self.record(numframes)
        now = self.clock()
        self.last_data = now
        if self.await_first:
            # 이 소리는 지금부터 len/sr 초 전부터의 것 — 그 앞의 빈 시간을 무음으로
            self.await_first = False
            gap = (now - len(data) / self.samplerate) - self.covered
            self.covered = now
            if gap > 0:
                self.gap_frames = int(round(gap * self.samplerate))
                self.pending = data
                log.warning("녹음을 %s 로 다시 이었습니다(끊긴 %.1f초는 무음으로 채움).",
                            self.cands[self.index].label, gap)
                return self.record(numframes)
            log.warning("녹음을 %s 로 다시 이었습니다.", self.cands[self.index].label)
            return data
        self.covered = now
        return data

    def close(self) -> None:
        self._close()


def _candidates(source, *, prefer_dshow: bool, alt: Callable | None,
                chain: Callable | None) -> list[_Candidate]:
    sc = _Candidate("soundcard", source)
    if chain is not None:
        rest = list(chain(source))
    elif alt is not None:                        # 테스트·예전 호출: 대체 하나
        a = alt(source)
        rest = [a] if a is not None else []
    else:
        rest = alternatives(source)
    rest_c = [_Candidate(getattr(r, "label", "ffmpeg DirectShow"), r) for r in rest]
    if prefer_dshow and rest_c and getattr(rest_c[0].dev, "tool", "") == "ffmpeg":
        return [rest_c[0], sc, *rest_c[1:]]
    return [sc, *rest_c]


@contextmanager
def open_recording(source, *, samplerate: int, channels, blocksize=None,
                   prefer_dshow: bool = False,
                   on_fallback: Callable | None = None,
                   alt: Callable | None = None, chain: Callable | None = None,
                   resilient: bool = False, **kw) -> Iterator:
    """source 로 녹음을 엽니다. 안 되면 사슬의 다음 방법으로.

    resilient=False(장치 시험): 처음 열리는 방법 하나로 그대로 읽습니다. 다 실패하면 방법마다
      이유가 적힌 RuntimeError — 빈 AssertionError 를 그대로 올리지 않습니다.
    resilient=True(녹음): 위에 더해 녹음 중 끊기면 다음 방법으로 바꾸고 끊긴 시간을 무음으로 채움.
    prefer_dshow(recording.audio.backend = "dshow"): ffmpeg 를 맨 앞에.
    """
    cands = _candidates(source, prefer_dshow=prefer_dshow, alt=alt, chain=chain)

    def switched(dev, why):
        if on_fallback:
            reason = "설정(backend = dshow)" if (prefer_dshow and dev is cands[0].dev
                                                and dev is not source) else why
            on_fallback(dev, reason)

    rr = ResilientRecorder(cands, samplerate=samplerate, channels=channels, blocksize=blocksize,
                           on_switch=switched, **kw)
    if prefer_dshow and cands[0].dev is not source:
        rr.events.append("설정(backend = dshow)")       # 첫 방법이 ffmpeg 라는 표시
    rr.begin()
    try:
        if resilient:
            yield rr
        else:
            yield rr.rec
    finally:
        rr.close()
