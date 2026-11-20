"""소리 받기 -> ffmpeg 인코딩 -> (내장 서버 | Icecast).

입력은 두 길이 있습니다(stream.capture).

  soundcard : 이 프로젝트의 녹음기와 같은 장치 선택(WASAPI 루프백 포함).
              PCM 을 ffmpeg 의 stdin 으로 흘립니다. Windows 에서 **스피커
              소리**를 보내려면 이 길밖에 없습니다.
  ffmpeg    : ffmpeg 가 장치를 직접 엽니다(터미널 녹음기와 같은 입력:
              dshow / avfoundation / pulse). soundcard 가 없는 맥·리눅스용.
  auto      : soundcard 를 import 할 수 있으면 soundcard, 아니면 ffmpeg.

출력도 두 길입니다(stream.server).

  builtin : ffmpeg stdout 을 읽어 hub.Hub 에 넘깁니다.
  icecast : ffmpeg 가 icecast://source:<비번>@host:port/mount 로 직접 밉니다.
            Icecast(오픈소스 스트리밍 서버)가 따로 떠 있어야 합니다.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from urllib.parse import quote

from ..logging_setup import get as get_logger
from ..recorder import terminal

log = get_logger("stream.capture")

CODECS = ("mp3", "aac")
CAPTURES = ("auto", "soundcard", "ffmpeg")
SERVERS = ("builtin", "icecast")


class CaptureError(Exception):
    pass


@dataclass
class EncodeSpec:
    codec: str = "mp3"
    bitrate_kbps: int = 96
    samplerate: int = 48000
    channels: int = 2


def _codec_args(spec: EncodeSpec) -> list[str]:
    if spec.codec == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{int(spec.bitrate_kbps)}k", "-f", "mp3"]
    if spec.codec == "aac":
        return ["-c:a", "aac", "-b:a", f"{int(spec.bitrate_kbps)}k", "-f", "adts"]
    raise CaptureError(f"알 수 없는 codec: {spec.codec!r} (가능: {', '.join(CODECS)})")


def icecast_url(*, host: str, port: int, mount: str, user: str, password: str) -> str:
    mount = "/" + mount.lstrip("/")
    return f"icecast://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{int(port)}{mount}"


def build_ffmpeg(spec: EncodeSpec, *, exe: str, input_args: list[str] | None,
                 output: str = "pipe:1", content_type: str = "") -> list[str]:
    """input_args 가 None 이면 stdin 의 s16le PCM 을 받습니다(soundcard 경로)."""
    if input_args is None:
        inp = ["-f", "s16le", "-ar", str(spec.samplerate), "-ac", str(spec.channels), "-i", "pipe:0"]
    else:
        inp = list(input_args)
    # 지연을 줄입니다: 입력 버퍼링을 끄고(입력 옵션) 출력은 바로바로 내보냅니다(출력 옵션).
    extra = ["-content_type", content_type] if content_type and output.startswith("icecast://") else []
    return [exe, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer", *inp,
            "-ac", str(spec.channels), "-ar", str(spec.samplerate),
            *_codec_args(spec), "-flush_packets", "1", *extra, output]


def resolve_capture(capture: str) -> str:
    if capture != "auto":
        return capture
    try:
        import soundcard  # type: ignore  # noqa: F401
    except Exception:
        return "ffmpeg"
    return "soundcard"


def ffmpeg_input_args(*, source: str, device: str, plat: str | None = None) -> list[str]:
    """터미널 녹음기와 같은 OS 별 입력. 안 되는 조합은 같은 말로 막습니다."""
    plan = terminal.Plan(source=source, device=device)
    try:
        return terminal._ffmpeg_input(plan, terminal.platform_key(plat))
    except terminal.TerminalAudioError as exc:
        raise CaptureError(str(exc)) from exc


class Encoder:
    """ffmpeg 프로세스 하나 + (필요하면) PCM 공급 스레드 + 출력 읽기 스레드."""

    def __init__(self, cmd: list[str], *, pcm_source=None, spec: EncodeSpec | None = None,
                 on_chunk=None, chunk: int = 4096) -> None:
        self.cmd = cmd
        self.pcm_source = pcm_source
        self.spec = spec or EncodeSpec()
        self.on_chunk = on_chunk
        self.chunk = chunk
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.error: str | None = None
        self.peak: float = 0.0

    def start(self) -> None:
        import os

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        log.debug("실행: %s", subprocess.list2cmdline(self.cmd))
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE if self.pcm_source is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if self.on_chunk is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        if self.pcm_source is not None:
            self._spawn(self._feed, "stream-feed")
        if self.on_chunk is not None:
            self._spawn(self._pump, "stream-pump")

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _feed(self) -> None:
        import numpy as np

        assert self.proc is not None and self.proc.stdin is not None
        frames = 2048
        try:
            with self.pcm_source.recorder(samplerate=self.spec.samplerate,
                                          channels=self.spec.channels,
                                          blocksize=frames) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=frames)
                    if data is None or len(data) == 0:
                        continue
                    max_val = float(np.max(np.abs(data)))
                    if max_val > self.peak:
                        self.peak = max_val
                    pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
                    self.proc.stdin.write(pcm.tobytes())
        except (BrokenPipeError, OSError, ValueError) as exc:
            if not self._stop.is_set():
                self.error = f"인코더로 소리를 보내지 못했습니다: {exc}"
                log.error(self.error)
        except Exception as exc:
            self.error = f"소리 장치 오류: {exc}"
            log.exception("소리 장치 오류")
        finally:
            try:
                self.proc.stdin.close()
            except Exception:
                pass

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        read = self.proc.stdout.read1 if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read
        while not self._stop.is_set():
            data = read(self.chunk)
            if not data:
                break
            self.on_chunk(data)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.error is None

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self.proc is not None and self.proc.poll() is None:
            if self.pcm_source is None:
                # 장치를 ffmpeg 가 직접 연 경우 stdin 이 없으니 닫아서 끝낼 수 없습니다.
                self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
        for t in self._threads:
            t.join(timeout=timeout)
