"""터미널 녹음기: FFmpeg · SoX · arecord 를 그대로 불러 소리만 받습니다.

`record start` 는 Windows 전용입니다(gdigrab + WASAPI 루프백). 이 모듈은
그 바깥을 맡습니다 — macOS·Linux 에서 소리만 받거나, Windows 에서도
화면 없이 예약 녹음만 하고 싶을 때입니다. 셋 다 명령줄 도구라 cron 이나
작업 스케줄러에 그대로 걸 수 있습니다.

  도구      지원 OS                  핵심 기능                     쓸 만한 때
  ffmpeg   Windows / macOS / Linux   -t 로 정확한 길이, mp3 인코딩   예약 시간제 녹음
  sox      Windows / macOS / Linux   silence 효과로 소리 감지 녹음   소리 날 때만 기록
  arecord  Linux                     설치 불필요, 초경량             서버·라즈베리파이 상시 녹음

명령을 만드는 함수(build_*)는 순수 함수입니다. 실행하지 않고 리스트만
돌려주므로 어느 OS 에서든 테스트할 수 있고, `--print` 로 사람이 그대로
복사해 쓸 수도 있습니다.

**시스템 소리(스피커로 나가는 소리)는 OS 마다 사정이 다릅니다.**
  - Linux(PulseAudio/PipeWire): `@DEFAULT_MONITOR@` 로 바로 잡힙니다.
  - macOS: OS 가 내주지 않습니다. BlackHole 같은 가상 장치를 깔고
    그 장치 번호를 device 에 적어야 합니다.
  - Windows: ffmpeg/sox 의 입력은 루프백을 모릅니다. 시스템 소리는
    `record start`(WASAPI 루프백)를 쓰세요. 여기서는 막고 그렇게 안내합니다.
"""

from __future__ import annotations

import collections
import os
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get as get_logger
from ..paths import safe_name, unique_path

log = get_logger("recorder.terminal")

BACKENDS = ("auto", "ffmpeg", "sox", "arecord", "silero")
SOURCES = ("mic", "system")
FORMATS = ("wav", "mp3", "flac")

# PulseAudio·PipeWire(pipewire-pulse) 모두 알아듣는 '기본 출력의 모니터'.
PULSE_MONITOR = "@DEFAULT_MONITOR@"


class TerminalAudioError(Exception):
    """사용자가 고칠 수 있는 문제. 메시지를 그대로 보여 줍니다."""


@dataclass
class Plan:
    """무엇을 어떻게 받을지. 설정과 명령줄 인자를 합친 결과입니다."""

    backend: str = "auto"
    source: str = "mic"
    device: str = ""
    seconds: float = 3600.0
    fmt: str = "wav"
    vad: bool = False
    vad_threshold: str = "3%"
    vad_start_sec: float = 0.1
    vad_stop_sec: float = 3.0
    samplerate: int = 44100
    channels: int = 2


def platform_key(name: str | None = None) -> str:
    """'windows' / 'macos' / 'linux'. 테스트에서 바꿔 끼울 수 있게 인자를 받습니다."""
    name = name or sys.platform
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "macos"
    return "linux"


def _secs(value: float) -> str:
    """60.0 -> '60', 1.5 -> '1.5'. 명령줄에 군더더기 소수점을 남기지 않습니다."""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"


def output_name(pattern: str, fmt: str, now: datetime | None = None) -> str:
    """'record_%Y%m%d_%H%M%S' -> 'record_20261116_090000.wav'.

    날짜·시각이 이름에 들어가므로 cron 이 매일 같은 명령을 불러도
    덮어쓰지 않고 쌓입니다. 셸의 $(date +...) 와 같은 일을 하지만
    Windows 배치 파일에서도 똑같이 동작합니다.
    """
    now = now or datetime.now()
    stem = safe_name(now.strftime(pattern or "record_%Y%m%d_%H%M%S"), fallback="record")
    return f"{stem}.{fmt}"


# ---------------------------------------------------------------------------
# 명령 만들기 (순수 함수)
# ---------------------------------------------------------------------------
def _ffmpeg_input(plan: Plan, plat: str) -> list[str]:
    if plat == "windows":
        if plan.source == "system":
            raise TerminalAudioError(
                "Windows 에서 ffmpeg 는 스피커 소리(루프백)를 직접 받지 못합니다.\n"
                "  시스템 소리는  python -m syncnote record start  를 쓰세요(WASAPI 루프백, 설치 불필요)."
            )
        if not plan.device:
            raise TerminalAudioError(
                "Windows 의 ffmpeg(dshow)는 장치 이름이 필요합니다.\n"
                "  ffmpeg -hide_banner -list_devices true -f dshow -i dummy\n"
                "  로 이름을 확인해 --device 또는 [recording.terminal] device 에 적으세요."
            )
        return ["-f", "dshow", "-i", f"audio={plan.device}"]

    if plat == "macos":
        if plan.source == "system" and not plan.device:
            raise TerminalAudioError(
                "macOS 는 스피커 소리를 내주지 않습니다. BlackHole 같은 가상 장치를 설치하고\n"
                "  ffmpeg -f avfoundation -list_devices true -i \"\"\n"
                "  로 그 장치 번호를 확인해 --device 에 적으세요(예: --device 2)."
            )
        index = plan.device or "0"
        # avfoundation 은 "영상:소리" 입니다. 앞을 비우면 소리만 받습니다.
        return ["-f", "avfoundation", "-i", index if index.startswith(":") else f":{index}"]

    device = plan.device or (PULSE_MONITOR if plan.source == "system" else "default")
    return ["-f", "pulse", "-i", device]


def build_ffmpeg(plan: Plan, out: Path, *, plat: str | None = None, exe: str = "ffmpeg") -> list[str]:
    """ffmpeg -f <드라이버> -i <장치> -t <초> <파일>.

    -t 로 길이를 정확히 끊어 스스로 끝납니다. 확장자가 mp3 면
    libmp3lame 으로 인코딩합니다.
    """
    plat = platform_key(plat)
    codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
             "flac": ["-c:a", "flac"],
             "wav": ["-c:a", "pcm_s16le"]}[plan.fmt]
    return [
        exe, "-hide_banner", "-loglevel", "warning", "-nostdin",
        *_ffmpeg_input(plan, plat),
        "-t", _secs(plan.seconds),
        "-ac", str(plan.channels), "-ar", str(plan.samplerate),
        *codec, str(out),
    ]


def _sox_input(plan: Plan, plat: str) -> list[str]:
    if plat == "windows" and plan.source == "system":
        raise TerminalAudioError(
            "Windows 에서 SoX 는 스피커 소리(루프백)를 받지 못합니다.\n"
            "  시스템 소리는  python -m syncnote record start  를 쓰세요."
        )
    if plat == "macos" and plan.source == "system" and not plan.device:
        raise TerminalAudioError(
            "macOS 는 스피커 소리를 내주지 않습니다. BlackHole 같은 가상 장치를 깔고 "
            "그 이름을 --device 에 적으세요(예: --device BlackHole)."
        )
    device = plan.device or (PULSE_MONITOR if plat == "linux" and plan.source == "system" else "")
    if not device:
        return ["-d"]  # 기본 입력 장치. `rec` 명령이 하는 일과 같습니다.
    driver = {"windows": "waveaudio", "macos": "coreaudio"}.get(plat)
    if driver is None:
        driver = "alsa" if device.startswith(("hw:", "plughw:")) else "pulseaudio"
    return ["-t", driver, device]


def build_sox(plan: Plan, out: Path, *, plat: str | None = None, exe: str = "sox") -> list[str]:
    """sox <입력> <파일> [silence ...] [trim 0 <초>].

    vad 가 켜져 있으면 README 예시와 같은 줄을 만듭니다.

        rec out.wav silence 1 0.1 3% 1 3.0 3%

    앞의 `1 0.1 3%` 는 '3% 이상 소리가 0.1초 이어지면 시작',
    뒤의 `1 3.0 3%` 는 '3% 아래로 3초 이어지면 끝' 입니다.
    아무 소리도 안 나면 영원히 기다리므로 trim 으로 상한을 둡니다.
    """
    plat = platform_key(plat)
    if plan.fmt == "mp3":
        raise TerminalAudioError(
            "SoX 는 배포판에 따라 mp3 를 쓰지 못합니다. --format wav 또는 flac 을 쓰세요."
        )
    cmd = [exe, "-q", *_sox_input(plan, plat),
           "-c", str(plan.channels), "-r", str(plan.samplerate), str(out)]
    if plan.vad:
        cmd += ["silence",
                "1", _secs(plan.vad_start_sec), plan.vad_threshold,
                "1", _secs(plan.vad_stop_sec), plan.vad_threshold]
    if plan.seconds > 0:
        cmd += ["trim", "0", _secs(plan.seconds)]
    return cmd


def build_arecord(plan: Plan, out: Path, *, plat: str | None = None, exe: str = "arecord") -> list[str]:
    """arecord -f cd -d <초> [-D 장치] <파일>.

    ALSA 의 기본 도구라 설치가 필요 없습니다. 대신 리눅스 전용이고,
    wav 만 쓰며, 시스템 소리는 ALSA 루프백 장치를 직접 줘야 합니다.
    """
    plat = platform_key(plat)
    if plat != "linux":
        raise TerminalAudioError("arecord 는 리눅스(ALSA) 전용입니다. ffmpeg 나 sox 를 쓰세요.")
    if plan.fmt != "wav":
        raise TerminalAudioError("arecord 는 wav 만 씁니다. --format wav 로 두세요.")
    if plan.source == "system" and not plan.device:
        raise TerminalAudioError(
            "arecord 는 스피커 소리를 바로 잡지 못합니다. snd-aloop 같은 루프백 장치를\n"
            "  --device 에 주거나(arecord -L 로 확인), ffmpeg/sox 백엔드를 쓰세요."
        )
    # -f cd = 16bit 44.1kHz 스테레오. 다른 값을 고르면 S16_LE 로 직접 지정합니다.
    if plan.samplerate == 44100 and plan.channels == 2:
        fmt = ["-f", "cd"]
    else:
        fmt = ["-f", "S16_LE", "-r", str(plan.samplerate), "-c", str(plan.channels)]
    cmd = [exe, "-q", *fmt]
    if plan.seconds > 0:
        # arecord 의 -d 는 정수 초만 받습니다. 모자라지 않게 올림합니다.
        cmd += ["-d", str(max(1, int(-(-float(plan.seconds) // 1))))]
    if plan.device:
        cmd += ["-D", plan.device]
    return [*cmd, str(out)]


BUILDERS = {"ffmpeg": build_ffmpeg, "sox": build_sox, "arecord": build_arecord}


# ---------------------------------------------------------------------------
# 도구 찾기
# ---------------------------------------------------------------------------
def find_tool(name: str) -> str | None:
    if name == "ffmpeg":
        from .ffmpeg_util import FFmpegMissing, find_ffmpeg

        try:
            return find_ffmpeg()
        except FFmpegMissing:
            return None
    if name == "sox":
        # PATH 말고도 setup 이 둔 tools\sox · winget 이 둔 곳까지(창을 다시 열지 않아도 찾게)
        from ..sox_fetch import find_sox

        return find_sox(check=False)
    env = os.environ.get(name.upper())
    if env and (shutil.which(env) or Path(env).exists()):
        return shutil.which(env) or env
    return shutil.which(name)


def available() -> dict[str, str | None]:
    return {name: find_tool(name) for name in ("ffmpeg", "sox", "arecord")}


def choose_backend(plan: Plan, found: dict[str, str | None], *, plat: str | None = None) -> str:
    """auto 를 실제 도구로 바꿉니다.

    소리 감지(vad)는 SoX 만 할 수 있으니 SoX 로 갑니다. 그 밖에는
    ffmpeg → sox → arecord 순입니다. ffmpeg 는 이 프로젝트가 이미
    설치하고 있으므로 대개 여기서 끝납니다.
    """
    plat = platform_key(plat)
    if plan.backend != "auto":
        return plan.backend
    if plan.vad:
        if found.get("sox"):
            return "sox"
        raise TerminalAudioError(
            "소리 감지 녹음(--vad)은 SoX 가 필요합니다.\n"
            "  Windows: https://sourceforge.net/projects/sox/  (설치 후 PATH 추가)\n"
            "  macOS  : brew install sox\n"
            "  Linux  : sudo apt install sox libsox-fmt-pulse"
        )
    order = ["ffmpeg", "sox"] + (["arecord"] if plat == "linux" else [])
    for name in order:
        if found.get(name):
            return name
    raise TerminalAudioError(
        f"녹음 도구를 찾지 못했습니다({', '.join(order)} 중 하나가 필요합니다). "
        "scripts/setup.bat 이 ffmpeg 를 받아 줍니다."
    )


def build(plan: Plan, out: Path, *, plat: str | None = None,
          found: dict[str, str | None] | None = None) -> tuple[str, list[str]]:
    """(쓰는 백엔드, 명령) 을 돌려줍니다. 실행은 하지 않습니다."""
    if plan.backend == "silero":
        raise TerminalAudioError("silero 백엔드는 파이썬 내부 링 버퍼를 사용하므로 CLI 명령을 만들지 않습니다.")
    if plan.vad and plan.backend not in ("auto", "sox"):
        raise TerminalAudioError("소리 감지 녹음(--vad)은 sox 백엔드에서만 됩니다.")
    found = available() if found is None else found
    backend = choose_backend(plan, found, plat=plat)
    exe = found.get(backend) or backend
    return backend, BUILDERS[backend](plan, out, plat=plat, exe=exe)


# ---------------------------------------------------------------------------
# Silero VAD + 링 버퍼 녹음
# ---------------------------------------------------------------------------
def record_vad_ring_buffer(
    out: Path,
    plan: Plan,
    *,
    vad_detector: Any = None,
    pcm_source: Any = None,
    max_wait_sec: float = 60.0,
    hangover_sec: float | None = None,
    pre_roll_chunks: int = 15,
) -> Path | None:
    """Silero VAD + 링 버퍼 기반 음성 감지 녹음.

    - 32ms 청크 단위로 오디오 수집
    - collections.deque(maxlen=15) 프리롤 버퍼로 발화 직전 480ms 오디오 보존 (첫 어절 잘림 방지)
    - 발화 감지 시 프리롤을 파일에 기록하고 녹음 개시
    - 발화 종료 후 hangover_chunks 동안 무음 지속 시 정상 종료
    - max_wait_sec 동안 발화가 전혀 없으면 타임아웃 종료
    - 파일 기록용 원본 PCM(int16)과 VAD 추론용 정규화 데이터(float32) 명확 분리
    """
    import numpy as np

    if vad_detector is None:
        from .vad import SileroVADDetector

        vad_detector = SileroVADDetector()
    if not getattr(vad_detector, "available", False):
        raise TerminalAudioError("Silero VAD 를 사용할 수 없습니다 (onnxruntime 또는 모델 파일 없음).")

    if pcm_source is None:
        from . import devices

        if plan.source == "system":
            pcm_source = devices.resolve_system_source(plan.device)
        else:
            pcm_source = devices.resolve_mic_source(plan.device)

    samplerate = int(plan.samplerate)
    channels = int(plan.channels)
    chunk_frames = max(1, int(round(samplerate * 0.032)))
    max_record_chunks = int(round(plan.seconds / 0.032)) if plan.seconds > 0 else 0

    if hangover_sec is None:
        hangover_sec = plan.vad_stop_sec if plan.vad_stop_sec > 0 else 1.5
    hangover_chunks = max(1, int(round(hangover_sec / 0.032)))

    pre_roll: collections.deque[bytes] = collections.deque(maxlen=pre_roll_chunks)

    vad_detector.reset()
    is_recording = False
    recorded_chunks = 0
    silence_chunks = 0
    wav = None
    started_wait = time.time()

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp_wav = out if plan.fmt == "wav" else out.with_suffix(".temp.wav")

    try:
        with pcm_source.recorder(samplerate=samplerate, channels=channels, blocksize=chunk_frames) as rec:
            while True:
                data = rec.record(numframes=chunk_frames)
                if data is None or len(data) == 0:
                    continue

                # 1. 파일 기록용 원본 PCM (int16) 데이터 분리
                if np.issubdtype(data.dtype, np.floating):
                    clipped = np.clip(data, -1.0, 1.0)
                    pcm_int16 = (clipped * 32767.0).astype(np.int16)
                elif np.issubdtype(data.dtype, np.integer):
                    pcm_int16 = data.astype(np.int16)
                else:
                    pcm_int16 = data.astype(np.int16)
                pcm_bytes = pcm_int16.tobytes()

                # 2. VAD 추론용 정규화 데이터 분리 (float32, 16kHz 모노, 512 샘플)
                mono = np.mean(data, axis=1) if data.ndim > 1 else data

                if np.issubdtype(mono.dtype, np.integer):
                    mono_f32 = mono.astype(np.float32) / 32768.0
                else:
                    mono_f32 = mono.astype(np.float32)

                if samplerate == 48000:
                    chunk_16k = mono_f32[::3]
                elif samplerate == 16000:
                    chunk_16k = mono_f32
                else:
                    step = max(1, round(samplerate / 16000))
                    chunk_16k = mono_f32[::step]

                if len(chunk_16k) > 512:
                    chunk_16k = chunk_16k[:512]
                elif len(chunk_16k) < 512:
                    chunk_16k = np.pad(chunk_16k, (0, 512 - len(chunk_16k)))

                prob = vad_detector.step(chunk_16k)
                speech = prob >= vad_detector.threshold

                if not is_recording:
                    if not speech:
                        pre_roll.append(pcm_bytes)
                        if max_wait_sec > 0 and (time.time() - started_wait) >= max_wait_sec:
                            log.warning("[vad] 대기 시간(%.1f초) 동안 발화가 감지되지 않았습니다.", max_wait_sec)
                            break
                    else:
                        is_recording = True
                        wav = wave.open(str(temp_wav), "wb")  # noqa: SIM115
                        wav.setnchannels(channels)
                        wav.setsampwidth(2)
                        wav.setframerate(samplerate)
                        for pre in pre_roll:
                            wav.writeframes(pre)
                        pre_roll.clear()
                        wav.writeframes(pcm_bytes)
                        recorded_chunks += 1
                        silence_chunks = 0
                else:
                    wav.writeframes(pcm_bytes)
                    recorded_chunks += 1
                    if speech:
                        silence_chunks = 0
                    else:
                        silence_chunks += 1
                        if silence_chunks >= hangover_chunks:
                            log.info("[vad] 발화 종료 감지 (연속 무음 %d청크, 약 %.1f초)",
                                     silence_chunks, silence_chunks * 0.032)
                            break
                    if max_record_chunks > 0 and recorded_chunks >= max_record_chunks:
                        log.info("[vad] 최대 녹음 시간(%.1f초) 도달", plan.seconds)
                        break
    finally:
        if wav is not None:
            try:
                wav.close()
            except Exception:
                pass

    if not is_recording or not temp_wav.exists() or temp_wav.stat().st_size <= 44:
        if temp_wav.exists():
            try:
                temp_wav.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    if plan.fmt != "wav" and temp_wav != out:
        from .ffmpeg_util import find_ffmpeg, run

        codec = {"mp3": ["-c:a", "libmp3lame", "-q:a", "4"],
                 "flac": ["-c:a", "flac"]}.get(plan.fmt, ["-c:a", "copy"])
        cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "warning", "-y",
               "-i", str(temp_wav), *codec, str(out)]
        try:
            run(cmd, timeout=30)
            temp_wav.unlink(missing_ok=True)
        except Exception as exc:
            log.warning("포맷 변환(%s) 실패, WAV 로 유지합니다: %s", plan.fmt, exc)
            return temp_wav

    return out


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def record_once(plan: Plan, out_dir: Path, *, pattern: str = "record_%Y%m%d_%H%M%S",
                found: dict[str, str | None] | None = None,
                vad_detector: Any = None,
                pcm_source: Any = None) -> Path | None:
    """한 파일을 녹음합니다. 받은 파일 경로, 아무것도 안 남았으면 None."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = unique_path(out_dir / output_name(pattern, plan.fmt))

    use_silero = False
    if plan.backend == "silero":
        use_silero = True
    elif plan.vad and plan.backend == "auto":
        if vad_detector is not None and getattr(vad_detector, "available", False):
            use_silero = True
        elif vad_detector is None:
            try:
                from .vad import SileroVADDetector

                detector = SileroVADDetector()
                if detector.available:
                    vad_detector = detector
                    use_silero = True
            except Exception:
                use_silero = False

    if use_silero:
        try:
            log.info("[silero-vad] 링 버퍼 기반 음성 감지 녹음 시작 -> %s", out)
            return record_vad_ring_buffer(out, plan, vad_detector=vad_detector, pcm_source=pcm_source)
        except TerminalAudioError:
            raise
        except Exception as exc:
            log.warning("[silero-vad] 녹음 중 오류(%s), CLI 백엔드로 폴백합니다.", exc)

    found = available() if found is None else found
    for backend, cmd, path in _attempts(plan, out, found):
        got = _run_one(backend, cmd, path, plan)
        if got is not None:
            return got
        path.unlink(missing_ok=True)                 # 빈 파일·반쪽 파일은 지우고 다음 도구로
    log.error("녹음 도구를 모두 시도했지만 파일을 만들지 못했습니다. 장치 이름과 도구 설치를 확인하세요"
              "(python -m syncnote audio tools).")
    return None


def _attempts(plan: Plan, out: Path, found: dict[str, str | None]):
    """(도구, 명령, 파일) 을 시도할 순서대로. 고른 도구가 먼저, 안 되면 나머지 중 이 녹음을
    만들 수 있는 도구(설치돼 있고 이 OS·형식·소리 감지를 지원)로 — 정규화 롤백.

    사용자가 도구를 직접 골랐으면(backend 가 auto 가 아님) 그 도구만 씁니다.
    """
    first, cmd = build(plan, out, found=found)
    yield first, cmd, out
    if plan.backend != "auto":
        return
    for name in ("ffmpeg", "sox", "arecord"):
        if name == first or not found.get(name):
            continue
        alt_out = unique_path(out)
        try:
            alt_cmd = BUILDERS[name](plan, alt_out, exe=found[name])
        except TerminalAudioError as exc:
            log.debug("[%s] 이 녹음에는 못 씀: %s", name, exc)
            continue
        log.warning("[%s] 로 다시 녹음합니다(앞 도구가 파일을 만들지 못함).", name)
        yield name, alt_cmd, alt_out


def _run_one(backend: str, cmd: list[str], out: Path, plan: Plan) -> Path | None:
    log.info("[%s] 녹음 시작 -> %s", backend, out)
    log.debug("실행: %s", subprocess.list2cmdline(cmd))
    # 소리 감지 녹음은 첫 소리가 언제 날지 모릅니다. 상한(trim)이 걸려
    # 있어도 '기다리는 시간' 은 그 밖이라 시간 제한을 두지 않습니다.
    timeout = None if plan.vad or plan.seconds <= 0 else float(plan.seconds) + 60
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        log.error("[%s] %.0f초가 지나도 끝나지 않아 멈췄습니다.", backend, timeout or 0)
        code = -1
    except FileNotFoundError as exc:
        log.error("[%s] 실행 파일을 찾지 못했습니다: %s", backend, exc)
        return None
    except KeyboardInterrupt:
        # 같은 콘솔의 자식도 Ctrl+C 를 받아 파일을 닫고 끝납니다. 사람이 멈춘 것이라
        # 다른 도구로 다시 녹음하지 않습니다.
        log.info("[%s] 사용자가 멈췄습니다. 받은 데까지 저장합니다.", backend)
        if not out.exists() or out.stat().st_size == 0:
            raise
        code = 0
    elapsed = time.monotonic() - started

    if not out.exists() or out.stat().st_size == 0:
        log.error("[%s] 파일이 만들어지지 않았습니다(code %s).", backend, code)
        return None
    if code not in (0, None):
        log.warning("[%s] 종료 코드 %s. 받은 파일은 남겨 둡니다: %s", backend, code, out)
    log.info("[%s] 녹음 종료: %.1f초, %s (%d KB)",
             backend, elapsed, out.name, out.stat().st_size // 1024)
    return out


def default_out_dir(recordings_root: Path, now: datetime | None = None) -> Path:
    """<data>/recordings/<날짜>/terminal. 기존 세션 폴더와 섞이지 않게 둡니다."""
    now = now or datetime.now()
    return Path(recordings_root) / f"{now:%Y-%m-%d}" / safe_name("terminal")
