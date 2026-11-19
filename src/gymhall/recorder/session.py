"""녹화 세션 = 세그먼트 여러 개.

왜 세그먼트로 쪼개나:
  1) GitHub 릴리스 에셋 상한 2GiB 를 자연스럽게 피합니다.
  2) 3시간 녹화 도중 노트북이 꺼져도 앞부분은 살아남습니다.
  3) 전사를 세그먼트 단위로 병렬/순차 처리할 수 있습니다.
  4) 녹화가 끝나는 즉시 조금씩 업로드할 수 있습니다.

대가:
  세그먼트 사이에 1초 안팎의 공백이 생깁니다. 강의 녹화에서는
  문제가 되지 않지만, 음악이나 연속 측정에는 맞지 않습니다.

다중 모니터 녹화:
  recording.video.monitors = [1, 2] 처럼 목록을 적으면 각 모니터를
  독립 프로세스로 동시에 녹화해 별도 파일로 저장합니다.
  파일명은 <stem>_mon1.<container>, <stem>_mon2.<container> 형식입니다.
  monitors 가 비어 있으면 기존 단일 monitor 설정을 그대로 씁니다.
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
from . import devices
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
    # 장치 무음으로 중단된 세그먼트. 로컬에는 두되 올리지는 않습니다 —
    # 소리 없는 영상이 백업된 것처럼 보이면 안 됩니다.
    broken: bool = False


_UNSET = object()


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
        stop_on_silence: bool = False,
        video_not_before: float = 0.0,
        realtime_queue: queue.Queue | None = None,
        on_segment_done: Callable[[Segment], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.layout = layout
        self.label = safe_name(label)
        # 이 녹화가 어느 과목인지. None 이면 과목 폴더 없이 예전 경로 그대로고,
        # 업로드도 하지 않습니다(어느 레포로 갈지 모르므로).
        self.course = course
        # 무음으로 멈출지. 수동 녹화는 멈추지 않고 경고만 합니다.
        self.stop_on_silence = bool(stop_on_silence)
        # 화면 캡처를 이 시각(epoch 초) 전에는 시작하지 않습니다.
        # pre-roll 이 수업 시작 2분 전부터 녹화를 켜는데, 그 2분 동안
        # 강의실 PC 앞에 앉은 사람의 화면이 그대로 찍혀 업로드됩니다.
        # 소리는 그때부터 받고(수업이 일찍 시작해도 안 놓치려고),
        # 화면만 정시에 시작합니다. mux 가 -itsoffset 으로 맞춥니다.
        self.video_not_before = float(video_not_before or 0.0)
        # 마이크는 세션당 한 번만 고릅니다. _UNSET 은 '아직 안 골랐다'
        # 이고 None 은 '골랐는데 쓸 게 없다' 입니다. 둘을 섞으면 매
        # 세그먼트마다 다시 시험해 그때마다 1.5초씩 소리가 빕니다.
        self._mic_source: object = _UNSET
        self.silence_stop_sec = float(cfg.get("recording.audio.silence_stop_sec", 900))
        self.silence_abort_sec = float(cfg.get("recording.audio.silence_abort_sec", 3600))
        # 장치 무음으로 중단된 세션은 올리지 않습니다.
        self.broken = False
        self.realtime_queue = realtime_queue
        self.on_segment_done = on_segment_done
        self._stop_event = threading.Event()
        self._install_signal_handlers()

    # -- 무음 판정 ---------------------------------------------------
    # "루프백은 잡히는데 소리가 0" 은 녹화가 쓸모없어지는 가장 흔한 길입니다.
    # 지금까지는 60초 뒤 경고 한 줄이 전부라, 끝나고 로그를 봐야 알았습니다.
    #
    # 트랙 하나만 보면 안 됩니다. 마이크만 조용한 것은 아주 흔합니다
    # (교수가 판서만 하는 5분, 시험 시간). **켜져 있는 트랙이 전부
    # 동시에** 조용할 때만 장치 문제로 봅니다.
    #
    # 수동 녹화(record start)에는 적용하지 않습니다. 사용자가 직접
    # 시작한 것을 코드가 멈추면 안 됩니다 — 경고만 합니다.
    def _silence_verdict(self, audio_rec) -> tuple[str, str] | None:
        if audio_rec is None or not self.stop_on_silence:
            return None
        tracks = [t for t in getattr(audio_rec, "results", []) or [] if not t.error]
        if not tracks:
            return None
        quiet = min((t.silent_for for t in tracks), default=0.0)
        if self.silence_abort_sec and quiet >= self.silence_abort_sec:
            return ("abort", f"모든 트랙이 {quiet / 60:.0f}분째 무음입니다. "
                             "장치 문제로 보고 세션을 중단합니다(업로드 안 함).")
        if self.silence_stop_sec and quiet >= self.silence_stop_sec:
            return ("stop", f"모든 트랙이 {quiet / 60:.0f}분째 무음입니다. "
                            "녹화를 멈춥니다. python -m gymhall devices 로 "
                            "장치를 확인하세요.")
        return None

    def _resolve_mic(self):
        """이 세션에서 쓸 마이크. 한 번만 고르고 그대로 씁니다.

        설정에 이름이 적혀 있으면 그게 유일한 진실입니다 — 시험하지
        않습니다. 사람이 고른 것을 코드가 뒤집으면 안 됩니다. 죽어
        있으면 녹음이 끝나고 'peak 가 정확히 0' 오류가 알려 줍니다.
        """
        if self._mic_source is not _UNSET:
            return self._mic_source

        cfg = self.cfg
        if not cfg.get("recording.audio.mic_enabled", True):
            self._mic_source = None
            return None

        explicit = str(cfg.get("recording.audio.mic_device", "") or "").strip()
        if explicit or not cfg.get("recording.audio.mic_auto_select", True):
            # 리졸버가 평소처럼 이름으로 찾습니다.
            self._mic_source = None
            return None

        mic, probes = devices.select_working_mic(
            duration_sec=float(cfg.get("recording.audio.mic_probe_sec", 1.5)),
            samplerate=int(cfg.get("recording.audio.samplerate", 48000)),
            channels=int(cfg.get("recording.audio.channels", 2)),
        )
        self._log_mic_choice(mic, probes)
        self._mic_source = mic
        return mic

    @staticmethod
    def _log_mic_choice(mic, probes) -> None:
        if not probes:
            log.warning("마이크를 하나도 찾지 못했습니다. 영상만 녹화됩니다.")
            return
        if all(p.dead for p in probes):
            # 하나만 0 이면 장치 문제, 전부 0 이면 권한 문제입니다.
            # 이 둘을 한 문장에 섞으면 고칠 곳을 알 수 없습니다.
            log.error(
                "마이크 %d개가 전부 신호 0 입니다(%s). 장치 문제가 아니라 "
                "Windows 설정 > 개인 정보 및 보안 > 마이크 > '데스크톱 앱이 "
                "마이크에 액세스하도록 허용' 이 꺼져 있을 가능성이 큽니다. "
                "그래도 '%s' 로 시도는 합니다.",
                len(probes), ", ".join(p.name for p in probes[:3]),
                getattr(mic, "name", "(없음)"),
            )
            return
        chosen = probes[-1]
        if len(probes) == 1:
            log.info("마이크: '%s' (최대레벨 %.5f)", chosen.name, chosen.peak)
            return
        log.warning(
            "기본 마이크 '%s' 는 신호가 정확히 0 이라 '%s' 로 바꿉니다"
            "(최대레벨 %.5f). 고정하려면 config.toml 의 mic_device 에 "
            "이름 일부를 적으세요.",
            probes[0].name, chosen.name, chosen.peak,
        )
        for p in probes[:-1]:
            log.info("  건너뜀: '%s'%s", p.name,
                     f" — {p.error}" if p.error else " — 신호 0")

    def _wait_for_video_start(self) -> None:
        """화면 캡처 시작 시각까지 기다립니다. 첫 세그먼트에서만 걸립니다.

        기다리는 동안 소리는 이미 녹음되고 있습니다. 그래서 수업이
        정시보다 일찍 시작해도 말은 놓치지 않습니다 — 놓치는 것은
        그 시간의 화면뿐이고, 그건 놓치는 게 맞습니다.
        """
        if not self.video_not_before:
            return
        remain = self.video_not_before - time.time()
        if remain <= 0:
            return
        log.info("화면 캡처는 %.0f초 뒤에 시작합니다(수업 전 화면은 찍지 않습니다). "
                 "소리는 이미 녹음 중입니다.", remain)
        deadline = self.video_not_before
        while time.time() < deadline and not self._stop_event.is_set():
            time.sleep(min(0.5, max(0.05, deadline - time.time())))
        # 한 번 켠 뒤에는 다음 세그먼트에서 또 기다리면 안 됩니다.
        self.video_not_before = 0.0

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

        # --- 다중 모니터 목록 결정 ---
        # monitors = [1, 2] 처럼 목록이 있으면 각 모니터를 독립 녹화합니다.
        # 비어 있으면 기존 단일 monitor 설정을 그대로 씁니다.
        monitors_list: list[int] = list(cfg.get("recording.video.monitors", []) or [])
        # 단일 monitor > 0 도 목록으로 올려서 코드를 하나로 씁니다.
        if not monitors_list:
            single = int(cfg.get("recording.video.monitor", 0))
            monitors_list = [single]  # 0 = 전체, 양수 = 그 화면

        # --- VideoRecorder 목록 구성 ---
        def _make_video_rec(mon_idx: int) -> VideoRecorder:
            return VideoRecorder(
                out_dir=work,
                backend=str(cfg.get("recording.video.backend", "gdigrab")),
                fps=int(cfg.get("recording.video.fps", 10)),
                scale_width=int(cfg.get("recording.video.scale_width", 0)),
                scale_height=int(cfg.get("recording.video.scale_height", 0)),
                preset=str(cfg.get("recording.video.preset", "veryfast")),
                crf=int(cfg.get("recording.video.crf", 30)),
                window_title=str(cfg.get("recording.video.window_title", "")),
                monitor=mon_idx,
                draw_mouse=bool(cfg.get("recording.video.draw_mouse", True)),
                container=container,
            )

        # (VideoRecorder, suffix) 쌍 목록
        # 모니터가 여러 개면 파일명에 _mon<N> suffix 를 붙입니다.
        multi = len(monitors_list) > 1
        video_recs: list[tuple[VideoRecorder, str]] = []
        if video_on:
            for mon_idx in monitors_list:
                suffix = f"_mon{mon_idx}" if multi else ""
                video_recs.append((_make_video_rec(mon_idx), suffix))

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
                silence_peak=float(cfg.get("recording.audio.silence_peak", 1e-5)),
                realtime_queue=self.realtime_queue,
                realtime_source=str(cfg.get("transcribe.source_track", "system")),
                mic_source=self._resolve_mic(),
            )
            audio_rec.start(stem)

        # 화면은 소리보다 늦게 켭니다(pre-roll 프라이버시).
        video_paths: list[tuple[Path | None, str]] = []  # (path, suffix)
        if video_recs:
            self._wait_for_video_start()
            if multi:
                # 여러 모니터를 병렬로 띄웁니다.
                errors_per_rec: list[Exception | None] = [None] * len(video_recs)
                started_paths: list[Path | None] = [None] * len(video_recs)

                def _start_one(i, vr, _suffix):
                    try:
                        started_paths[i] = vr.start(f"{stem}{_suffix}")
                    except Exception as e:
                        errors_per_rec[i] = e

                threads = [
                    threading.Thread(target=_start_one, args=(i, vr, sfx), daemon=True)
                    for i, (vr, sfx) in enumerate(video_recs)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                for i, ((_vr, sfx), err) in enumerate(zip(video_recs, errors_per_rec, strict=True)):
                    if err is not None:
                        log.error("모니터 %s 녹화 시작 실패: %s", sfx or "(전체)", err)
                        result.warnings.append(f"세그먼트 {index} 모니터{sfx} 시작 실패: {err}")
                        video_recs[i] = (None, sfx)  # type: ignore[assignment]
                    video_paths.append((started_paths[i], sfx))
            else:
                # 단일 모니터 (기존 경로)
                video_rec, sfx = video_recs[0]
                try:
                    p = video_rec.start(f"{stem}{sfx}")
                    video_paths.append((p, sfx))
                except Exception as exc:
                    log.error("화면 녹화 시작 실패: %s", exc)
                    result.warnings.append(f"세그먼트 {index} 화면 시작 실패: {exc}")
                    video_paths.append((None, sfx))
                    video_recs = []

        # --- 대기 ---
        end_at = time.time() + minutes * 60
        log.info("세그먼트 %d 녹화 중 (%.1f분)", index, minutes)
        while time.time() < end_at and not self._stop_event.is_set():
            time.sleep(0.5)
            # 실행 중인 VideoRecorder 중 하나라도 죽으면 중단합니다.
            for vr, sfx in video_recs:
                if vr is not None and not vr.is_running:
                    log.error("ffmpeg 가 예기치 않게 종료됐습니다%s. 세그먼트를 마무리합니다.",
                              f" (모니터{sfx})" if multi else "")
                    result.warnings.append(f"세그먼트 {index}: ffmpeg 비정상 종료{sfx}")
                    self._stop_event.set()
                    break
            if self._stop_event.is_set():
                break
            verdict = self._silence_verdict(audio_rec)
            if verdict:
                result.warnings.append(f"세그먼트 {index}: {verdict[1]}")
                log.error("%s", verdict[1])
                if verdict[0] == "abort":
                    self.broken = True
                self._stop_event.set()
                break

        # --- 정지 ---
        audio_results = audio_rec.stop() if audio_rec else []
        audio_started = audio_rec.started_at if audio_rec else 0.0

        # 모든 VideoRecorder 를 병렬로 멈춥니다.
        stopped_video_paths: list[tuple[Path | None, str]] = []
        if video_recs:
            if multi:
                result_holders: list[Path | None] = [None] * len(video_recs)

                def _stop_one(i, vr):
                    result_holders[i] = vr.stop() if vr is not None else None

                stop_threads = [
                    threading.Thread(target=_stop_one, args=(i, vr), daemon=True)
                    for i, (vr, _) in enumerate(video_recs)
                ]
                for t in stop_threads:
                    t.start()
                for t in stop_threads:
                    t.join()
                stopped_video_paths = [(result_holders[i], sfx) for i, (_, sfx) in enumerate(video_recs)]
            else:
                vr, sfx = video_recs[0]
                stopped_video_paths = [(vr.stop() if vr is not None else None, sfx)]
        else:
            stopped_video_paths = [(None, sfx) for _, sfx in video_paths]

        for track in audio_results:
            if track.error:
                result.warnings.append(f"세그먼트 {index} {track.name}: {track.error}")
            elif track.silent:
                result.warnings.append(
                    f"세그먼트 {index} {track.name}: 전 구간 무음 (장치 설정 확인 필요)"
                )

        # --- 합치기: 모니터마다 독립 파일 ---
        mux_inputs = [
            MuxInput(t.path, t.name, t.started_at or audio_started)
            for t in audio_results if t.path.exists() and not t.error
        ]
        wav_copies = {t.name: t.path for t in audio_results if t.path.exists()}
        keep_wav = bool(cfg.get("transcribe.enabled", True))

        # 모니터가 여러 개면 모니터마다 mux, 하나면 기존대로.
        final_paths: list[Path] = []
        for video_path, sfx in stopped_video_paths:
            video_started = 0.0
            if video_recs:
                for vr, s in video_recs:
                    if s == sfx and vr is not None:
                        video_started = vr.started_at
                        break
            final = out_dir / f"{stem}{sfx}.{container}"
            try:
                mux(
                    final, video_path, video_started, mux_inputs,
                    layout=str(cfg.get("recording.audio.track_layout", "separate")),
                    delete_video=True,
                    delete_audio=False,  # 마지막 mux 뒤에 한 번만 지웁니다
                )
                final_paths.append(final)
            except Exception as exc:
                log.error("합치기 실패%s: %s", f" (모니터{sfx})" if multi else "", exc)
                result.warnings.append(f"세그먼트 {index} 합치기 실패{sfx}: {exc}")

        # WAV 삭제 (전사 불필요) or 이동
        if not keep_wav:
            for t in audio_results:
                if t.path.exists():
                    try:
                        t.path.unlink(missing_ok=True)
                    except OSError:
                        pass
        else:
            audio_dir = out_dir / "audio"
            audio_dir.mkdir(exist_ok=True)
            moved: dict[str, Path] = {}
            for name, path in wav_copies.items():
                if not path.exists():
                    continue
                target = audio_dir / path.name
                try:
                    path.replace(target)
                    moved[name] = target
                except OSError:
                    moved[name] = path
            wav_copies = moved

        # 작업 폴더 정리
        try:
            leftover = list(work.iterdir())
            if leftover:
                log.debug("작업 폴더에 %d개 파일이 남았습니다: %s", len(leftover), work)
            else:
                work.rmdir()
        except OSError as exc:
            log.debug("작업 폴더 정리 생략: %s", exc)

        from .ffmpeg_util import duration_of

        # 대표 파일(첫 번째 모니터)로 길이를 잽니다.
        ref_path = final_paths[0] if final_paths else None
        seconds = duration_of(ref_path) if ref_path else max(0.0, time.time() - (audio_started or time.time()))

        # 합친 파일이 하나면 기존 Segment 그대로, 여러 개면 목록 중 첫 번째로.
        primary_final = ref_path if ref_path else (out_dir / f"{stem}.{container}")
        log.info("세그먼트 %d 완료: %s (%.1f분)%s", index, primary_final.name, seconds / 60,
                 f" + {len(final_paths) - 1}개 추가 파일" if len(final_paths) > 1 else "")
        return Segment(index, primary_final, now, seconds,
                       [t.name for t in audio_results], wav_copies,
                       broken=self.broken)
