"""오디오 캡처.

시스템 소리와 마이크를 '각각 다른 스레드'에서 독립적으로 받아
각각 WAV 로 씁니다. 나중에 ffmpeg 가 별도 트랙으로 묶습니다.

왜 ffmpeg 의 dshow 를 안 쓰나:
  Windows 는 DirectShow 로 '스피커로 나가는 소리' 를 기본 제공하지
  않습니다. 잡으려면 스테레오 믹스(지원 안 하는 메인보드 많음)나
  VB-Cable 같은 가상 드라이버를 깔아야 합니다. 이동하며 여러 PC 에서
  쓴다는 요구사항과 정면으로 충돌합니다.
  soundcard 는 WASAPI 루프백을 직접 호출하므로 설치가 필요 없습니다.
"""

from __future__ import annotations

import queue
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

from ..logging_setup import get as get_logger
from . import devices

log = get_logger("recorder.audio")

# 한 번에 읽어올 프레임 수. 작을수록 지연이 짧고 CPU 를 더 씁니다.
BLOCK_FRAMES = 4096


def _db_to_gain(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


@dataclass
class TrackResult:
    name: str
    path: Path
    samplerate: int = 48000
    frames: int = 0
    started_at: float = 0.0
    peak: float = 0.0
    silent: bool = False
    error: str | None = None
    # 지금 이 순간까지 이어진 무음 길이(초). 세션이 트랙들을 모아
    # '모두 동시에 무음인가' 를 판단하는 데 씁니다. 트랙 하나만 보면
    # 안 됩니다 — 마이크만 조용한 것은 아주 흔한 일입니다.
    silent_for: float = 0.0

    @property
    def seconds(self) -> float:
        # 48000 을 그대로 쓰면 44100 설정에서 길이가 8% 틀어집니다.
        if not self.frames or not self.samplerate:
            return 0.0
        return self.frames / self.samplerate


class AudioTrack(threading.Thread):
    """장치 하나를 WAV 하나로 녹음하는 스레드."""

    def __init__(
        self,
        name: str,
        source,
        out_path: Path,
        *,
        samplerate: int = 48000,
        channels: int = 2,
        gain_db: float = 0.0,
        silence_warn_sec: float = 60.0,
        silence_peak: float = 1e-5,
        tap: queue.Queue | None = None,
    ) -> None:
        super().__init__(name=f"audio-{name}", daemon=True)
        # 주의: 이 필드 이름을 `_stop` 으로 바꾸지 마세요.
        # threading.Thread 는 Python 3.12 까지 내부 메서드 `_stop()` 을 가지고
        # 있어서, 같은 이름의 Event 를 넣으면 join() 이
        #   TypeError: 'Event' object is not callable
        # 로 터집니다. 3.13 에서는 그 메서드가 없어져 증상이 안 나타나므로
        # 개발 환경에서는 멀쩡하고 사용자 PC 에서만 깨집니다.
        self.track_name = name
        self.source = source
        self.out_path = Path(out_path)
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.gain = _db_to_gain(gain_db)
        self.silence_warn_sec = float(silence_warn_sec)
        self.silence_peak = float(silence_peak)
        self.tap = tap  # 실시간 전사로 흘려보낼 큐 (없으면 None)
        self._stop_event = threading.Event()
        self.result = TrackResult(name=name, path=self.out_path, samplerate=self.samplerate)

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # noqa: C901 - 스트리밍 루프라 분기가 많습니다
        import numpy as np

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        silent_for = 0.0
        warned = False

        try:
            # 녹음이 끝날 때까지 열어 두는 핸들입니다(finally 에서 닫습니다).
            # with 로 감싸면 스트리밍 루프를 전부 들여써야 해서 읽기 어려워집니다.
            wav = wave.open(str(self.out_path), "wb")  # noqa: SIM115
        except OSError as exc:
            self.result.error = f"WAV 파일 생성 실패: {exc}"
            log.error("[%s] %s", self.track_name, self.result.error)
            return

        try:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)  # int16
            wav.setframerate(self.samplerate)

            with self.source.recorder(samplerate=self.samplerate,
                                      channels=self.channels,
                                      blocksize=BLOCK_FRAMES) as rec:
                self.result.started_at = time.time()
                log.info("[%s] 녹음 시작 -> %s", self.track_name, self.out_path.name)

                while not self._stop_event.is_set():
                    data = rec.record(numframes=BLOCK_FRAMES)
                    if data is None or len(data) == 0:
                        continue

                    if self.gain != 1.0:
                        data = data * self.gain

                    peak = float(np.max(np.abs(data))) if data.size else 0.0
                    self.result.peak = max(self.result.peak, peak)

                    block_sec = len(data) / self.samplerate
                    if peak < self.silence_peak:
                        silent_for += block_sec
                        self.result.silent_for = silent_for
                        if not warned and silent_for >= self.silence_warn_sec:
                            log.warning(
                                "[%s] %.0f초 동안 무음입니다. 장치 선택이 잘못됐을 수 있습니다. "
                                "python -m gymhall devices 로 확인하세요.",
                                self.track_name, silent_for,
                            )
                            warned = True
                    else:
                        silent_for = 0.0
                        self.result.silent_for = 0.0

                    clipped = np.clip(data, -1.0, 1.0)
                    pcm = (clipped * 32767.0).astype("<i2")
                    wav.writeframes(pcm.tobytes())
                    self.result.frames += len(data)

                    if self.tap is not None:
                        try:
                            self.tap.put_nowait(clipped.copy())
                        except queue.Full:
                            pass  # 실시간 전사가 밀리면 버립니다. 녹음이 우선입니다.

        except Exception as exc:
            self.result.error = str(exc)
            log.exception("[%s] 녹음 중 오류", self.track_name)
        finally:
            try:
                wav.close()
            except Exception:
                pass
            self.result.silent = self.result.peak < self.silence_peak
            log.info(
                "[%s] 녹음 종료: %.1f초, 최대레벨 %.3f%s",
                self.track_name, self.result.seconds, self.result.peak,
                "  ** 전 구간 무음 **" if self.result.silent else "",
            )


@dataclass
class AudioRecorder:
    """시스템 + 마이크 트랙을 함께 관리합니다."""

    out_dir: Path
    samplerate: int = 48000
    channels: int = 2
    system_enabled: bool = True
    system_device: str = ""
    system_gain_db: float = 0.0
    mic_enabled: bool = True
    mic_device: str = ""
    mic_gain_db: float = 0.0
    silence_warn_sec: float = 60.0
    silence_peak: float = 1e-5
    realtime_queue: queue.Queue | None = None
    realtime_source: str = "system"

    tracks: list[AudioTrack] = field(default_factory=list)

    def start(self, stem: str) -> None:
        self.tracks = []
        specs = []
        if self.system_enabled:
            specs.append(("system", self.system_device, self.system_gain_db,
                          devices.resolve_system_source))
        if self.mic_enabled:
            specs.append(("mic", self.mic_device, self.mic_gain_db,
                          devices.resolve_mic_source))

        for name, device_name, gain, resolver in specs:
            try:
                source = resolver(device_name)
            except Exception as exc:
                log.error("[%s] 장치를 열 수 없습니다: %s", name, exc)
                continue
            tap = self.realtime_queue if (
                self.realtime_queue is not None
                and (self.realtime_source == name or self.realtime_source == "both")
            ) else None
            track = AudioTrack(
                name, source, Path(self.out_dir) / f"{stem}.{name}.wav",
                samplerate=self.samplerate, channels=self.channels, gain_db=gain,
                silence_warn_sec=self.silence_warn_sec,
                silence_peak=self.silence_peak, tap=tap,
            )
            self.tracks.append(track)
            track.start()

        if not self.tracks:
            log.error("오디오 트랙을 하나도 시작하지 못했습니다. 영상만 녹화됩니다.")

    def stop(self, timeout: float = 10.0) -> list[TrackResult]:
        for track in self.tracks:
            track.stop()
        for track in self.tracks:
            track.join(timeout=timeout)
        return [t.result for t in self.tracks]

    @property
    def results(self) -> list[TrackResult]:
        """녹음이 '도는 중' 에도 트랙 상태를 봅니다.

        stop() 은 끝난 뒤에만 결과를 주므로, 세션이 중간에
        '전 트랙이 몇 분째 무음인가' 를 묻는 데는 쓸 수 없습니다.
        TrackResult 는 스레드가 제자리에서 갱신하는 객체라
        여기서 그대로 내보내면 현재 값이 보입니다.

        주의: 이 property 를 지우거나 이름을 바꾸면
        RecordingSession._silence_verdict 의 getattr 가 조용히
        빈 리스트를 받아 무음 감지가 통째로 죽습니다.
        """
        return [t.result for t in self.tracks]

    @property
    def started_at(self) -> float:
        times = [t.result.started_at for t in self.tracks if t.result.started_at]
        return min(times) if times else 0.0
