"""녹화 세션 = 세그먼트 여러 개.

왜 세그먼트로 쪼개나:
  1) GitHub 릴리스 에셋 상한 2GiB 를 자연스럽게 피합니다.
  2) 3시간 녹화 도중 노트북이 꺼져도 앞부분은 살아남습니다.
  3) 전사를 세그먼트 단위로 병렬/순차 처리할 수 있습니다.
  4) 녹화가 끝나는 즉시 조금씩 업로드할 수 있습니다.

대가:
  세그먼트 사이에 1초 안팎의 공백이 생깁니다. 강의 녹화에서는
  문제가 되지 않지만, 음악이나 연속 측정에는 맞지 않습니다.
"""

from __future__ import annotations

import queue
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger
from ..paths import Layout, safe_name
from .audio import AudioRecorder
from .mux import MuxInput, mux
from .video import VideoRecorder

log = get_logger("recorder.session")


@dataclass
class Segment:
    index: int
    path: Path
    started_at: datetime
    seconds: float
    audio_tracks: list[str] = field(default_factory=list)
    wav_paths: dict[str, Path] = field(default_factory=dict)


@dataclass
class SessionResult:
    label: str
    started_at: datetime
    ended_at: datetime | None = None
    directory: Path | None = None
    segments: list[Segment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.segments)

    def summary(self) -> str:
        mins = self.total_seconds / 60
        return (f"{self.label}: 세그먼트 {len(self.segments)}개, "
                f"총 {mins:.1f}분, 폴더 {self.directory}")


class RecordingSession:
    def __init__(
        self,
        cfg,
        layout: Layout,
        label: str,
        *,
        course=None,
        realtime_queue: queue.Queue | None = None,
        on_segment_done: Callable[[Segment], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.layout = layout
        self.label = safe_name(label)
        # 이 녹화가 어느 과목인지. None 이면 과목 폴더 없이 예전 경로 그대로고,
        # 업로드도 하지 않습니다(어느 레포로 갈지 모르므로).
        self.course = course
        self.realtime_queue = realtime_queue
        self.on_segment_done = on_segment_done
        self._stop_event = threading.Event()
        self._install_signal_handlers()

    # -- 종료 신호 ---------------------------------------------------
    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("종료 신호(%s)를 받았습니다. 현재 세그먼트를 마무리합니다.", signum)
            self._stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass  # 메인 스레드가 아니면 무시

    def request_stop(self) -> None:
        self._stop_event.set()

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    # -- 실행 --------------------------------------------------------
    def run(self, *, duration_minutes: float | None = None) -> SessionResult:
        cfg = self.cfg
        started = datetime.now()
        out_dir = self.layout.session_dir(started, self.course, self.label)
        out_dir.mkdir(parents=True, exist_ok=True)

        result = SessionResult(label=self.label, started_at=started, directory=out_dir)

        max_minutes = float(duration_minutes or cfg.get("recording.max_duration_minutes", 240))
        seg_minutes = float(cfg.get("recording.segment_minutes", 30))
        if seg_minutes <= 0:
            seg_minutes = max_minutes
        seg_minutes = min(seg_minutes, max_minutes)

        container = str(cfg.get("recording.container", "mkv"))
        video_on = bool(cfg.get("recording.video.enabled", True))
        audio_on = bool(cfg.get("recording.audio.enabled", True))

        log.info("=" * 58)
        log.info("녹화 시작: %s", self.label)
        log.info("  폴더   : %s", out_dir)
        log.info("  세그먼트: %g분  /  최대: %g분", seg_minutes, max_minutes)
        log.info("  영상=%s  오디오=%s", "켬" if video_on else "끔", "켬" if audio_on else "끔")
        log.info("  Ctrl+C 또는 stop 명령으로 안전하게 중단할 수 있습니다.")
        log.info("=" * 58)

        deadline = time.time() + max_minutes * 60
        index = 0

        while not self._stop_event.is_set() and time.time() < deadline:
            index += 1
            remaining_min = (deadline - time.time()) / 60
            this_len = min(seg_minutes, remaining_min)
            if this_len <= 0.05:
                break
            try:
                segment = self._record_segment(index, out_dir, this_len, container,
                                               video_on, audio_on, result)
            except Exception as exc:
                log.exception("세그먼트 %d 실패", index)
                result.warnings.append(f"세그먼트 {index} 실패: {exc}")
                # 장치 문제라면 계속 돌려도 똑같이 실패하므로 멈춥니다.
                break
            if segment is not None:
                result.segments.append(segment)
                if self.on_segment_done:
                    try:
                        self.on_segment_done(segment)
                    except Exception:
                        log.exception("세그먼트 후처리 콜백 실패")

        result.ended_at = datetime.now()
        log.info("=" * 58)
        log.info("녹화 종료: %s", result.summary())
        for warning in result.warnings:
            log.warning("  ! %s", warning)
        log.info("=" * 58)
        return result

    # -- 세그먼트 하나 -----------------------------------------------
    def _record_segment(self, index: int, out_dir: Path, minutes: float, container: str,
                        video_on: bool, audio_on: bool, result: SessionResult) -> Segment | None:
        cfg = self.cfg
        now = datetime.now()
        stem = f"{now:%Y%m%d_%H%M%S}_part{index:03d}"
        work = out_dir / ".work"
        work.mkdir(parents=True, exist_ok=True)

        video_rec = None
        video_path = None
        if video_on:
            video_rec = VideoRecorder(
                out_dir=work,
                backend=str(cfg.get("recording.video.backend", "gdigrab")),
                fps=int(cfg.get("recording.video.fps", 10)),
                scale_width=int(cfg.get("recording.video.scale_width", 0)),
                scale_height=int(cfg.get("recording.video.scale_height", 0)),
                preset=str(cfg.get("recording.video.preset", "veryfast")),
                crf=int(cfg.get("recording.video.crf", 30)),
                window_title=str(cfg.get("recording.video.window_title", "")),
                draw_mouse=bool(cfg.get("recording.video.draw_mouse", True)),
                container=container,
            )
            video_path = video_rec.start(stem)

        audio_rec = None
        if audio_on:
            audio_rec = AudioRecorder(
                out_dir=work,
                samplerate=int(cfg.get("recording.audio.samplerate", 48000)),
                channels=int(cfg.get("recording.audio.channels", 2)),
                system_enabled=bool(cfg.get("recording.audio.system_enabled", True)),
                system_device=str(cfg.get("recording.audio.system_device", "")),
                system_gain_db=float(cfg.get("recording.audio.system_gain_db", 0.0)),
                mic_enabled=bool(cfg.get("recording.audio.mic_enabled", True)),
                mic_device=str(cfg.get("recording.audio.mic_device", "")),
                mic_gain_db=float(cfg.get("recording.audio.mic_gain_db", 0.0)),
                silence_warn_sec=float(cfg.get("recording.audio.silence_warn_sec", 60)),
                realtime_queue=self.realtime_queue,
                realtime_source=str(cfg.get("transcribe.source_track", "system")),
            )
            audio_rec.start(stem)

        # --- 대기 ---
        end_at = time.time() + minutes * 60
        log.info("세그먼트 %d 녹화 중 (%.1f분)", index, minutes)
        while time.time() < end_at and not self._stop_event.is_set():
            time.sleep(0.5)
            if video_rec is not None and not video_rec.is_running:
                log.error("ffmpeg 가 예기치 않게 종료됐습니다. 세그먼트를 마무리합니다.")
                result.warnings.append(f"세그먼트 {index}: ffmpeg 비정상 종료")
                break

        # --- 정지 ---
        audio_results = audio_rec.stop() if audio_rec else []
        audio_started = audio_rec.started_at if audio_rec else 0.0
        video_started = video_rec.started_at if video_rec else 0.0
        if video_rec:
            video_path = video_rec.stop()

        for track in audio_results:
            if track.error:
                result.warnings.append(f"세그먼트 {index} {track.name}: {track.error}")
            elif track.silent:
                result.warnings.append(
                    f"세그먼트 {index} {track.name}: 전 구간 무음 (장치 설정 확인 필요)"
                )

        # --- 합치기 ---
        final = out_dir / f"{stem}.{container}"
        mux_inputs = [
            MuxInput(t.path, t.name, t.started_at or audio_started)
            for t in audio_results if t.path.exists() and not t.error
        ]
        wav_copies = {t.name: t.path for t in audio_results if t.path.exists()}

        keep_wav = bool(cfg.get("transcribe.enabled", True))
        try:
            mux(
                final, video_path, video_started, mux_inputs,
                layout=str(cfg.get("recording.audio.track_layout", "separate")),
                delete_video=True,          # 합친 파일에 그대로 들어갔습니다
                delete_audio=not keep_wav,  # 전사에 쓸 거면 남깁니다
            )
        except Exception as exc:
            log.error("합치기 실패: %s", exc)
            result.warnings.append(f"세그먼트 {index} 합치기 실패: {exc}")
            # 합치기에 실패해도 원본은 남겨 둡니다. 데이터가 더 중요합니다.
            return Segment(index, work, now, (time.time() - (video_started or time.time())),
                           [t.name for t in audio_results], wav_copies)

        # 전사를 위해 WAV 를 세그먼트 폴더로 옮깁니다.
        moved: dict[str, Path] = {}
        if keep_wav:
            audio_dir = out_dir / "audio"
            audio_dir.mkdir(exist_ok=True)
            for name, path in wav_copies.items():
                if not path.exists():
                    continue
                target = audio_dir / path.name
                try:
                    path.replace(target)
                    moved[name] = target
                except OSError:
                    moved[name] = path

        # 작업 폴더 정리. 남은 파일이 있으면 지우지 않고 남겨 둡니다
        # (합치기가 실패했을 때의 원본일 수 있습니다).
        try:
            leftover = list(work.iterdir())
            if leftover:
                log.debug("작업 폴더에 %d개 파일이 남았습니다: %s", len(leftover), work)
            else:
                work.rmdir()
        except OSError as exc:
            log.debug("작업 폴더 정리 생략: %s", exc)

        from .ffmpeg_util import duration_of

        seconds = duration_of(final) or max(0.0, time.time() - (video_started or time.time()))
        log.info("세그먼트 %d 완료: %s (%.1f분)", index, final.name, seconds / 60)
        return Segment(index, final, now, seconds,
                       [t.name for t in audio_results], moved or wav_copies)
