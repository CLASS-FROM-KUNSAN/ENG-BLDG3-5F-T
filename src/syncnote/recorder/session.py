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
from . import autogain, devices
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
        meta=None,
    ) -> None:
        self.cfg = cfg
        self.layout = layout
        self.label = safe_name(label)
        # 파일 안에 넣을 메타데이터(recorder.meta.RecordingMeta). 안 주면 최소한
        # 이름·시각·'수동' 만이라도 남깁니다 — 모든 녹화 파일에 항상 남기는 것이 원칙.
        if meta is None:
            from .meta import RecordingMeta

            meta = RecordingMeta(label=label, trigger="manual")
        self.meta = meta
        # 이 녹화가 어느 과목인지. None 이면 과목 폴더 없이 예전 경로 그대로고,
        # 업로드도 하지 않습니다(어느 레포로 갈지 모르므로).
        self.course = course
        # 긴 무음을 경고할지(예약·감지 녹화). 어느 경우든 무음 때문에 녹화를 멈추거나
        # 업로드를 막지 않습니다 — 경고만 합니다(이름은 예전 그대로).
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
        self._mic_fallback_count = 0
        self.use_vad = bool(cfg.get("recording.audio.use_vad", False))
        self._vad_detector = None
        if self.use_vad:
            try:
                from .vad import SileroVADDetector

                self._vad_detector = SileroVADDetector(cfg.get("recording.audio.vad_model_path", ""))
            except Exception as exc:
                log.warning("VAD 감지기 초기화 실패(진폭 판정으로 폴백): %s", exc)

        # 무음 경고 시점(초). 예전에는 여기서 녹화를 멈추고(stop) 업로드를 막았지만(abort),
        # 무음이어도 녹화·업로드는 계속합니다(조용한 시간·마이크 늦게 꽂음·장치 고장도
        # 기록은 남아야 함). 키 이름은 기존 config.toml 과 맞추려고 그대로 둡니다.
        self.silence_stop_sec = float(cfg.get("recording.audio.silence_stop_sec", 900))
        self.silence_abort_sec = float(cfg.get("recording.audio.silence_abort_sec", 3600))
        self._silence_warned: str | None = None
        # 무음 시간은 트랙(세그먼트)마다 처음부터 셉니다. 세그먼트가 바뀌어도 이어서 세려고
        # '앞 세그먼트 끝까지 이어진 무음' 을 들고 갑니다(이게 없으면 30분 세그먼트에서
        # 60분 경고가 한 번도 나오지 않았음).
        self._quiet_carry = 0.0
        self._segment_t0 = 0.0
        # 화면 ffmpeg 가 곧바로 죽은 횟수(연속). 반복되면 그 세그먼트는 소리만 녹음.
        self._video_quick_deaths = 0
        self._video_died = False
        # 예전 '장치 무음으로 중단된 세션(업로드 안 함)' 표시. 이제 무음으로 세우지 않으므로 늘 False.
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
    # 무음이어도 녹화를 멈추거나 업로드를 막지 않습니다. 오래 조용하면 경고만 남깁니다
    # ("long" = silence_stop_sec, "very_long" = silence_abort_sec). 수동 녹화(record start)는
    # 경고도 하지 않습니다.
    def _silence_verdict(self, audio_rec) -> tuple[str, str] | None:
        if audio_rec is None or not self.stop_on_silence:
            return None
        tracks = [t for t in getattr(audio_rec, "results", []) or [] if not t.error]
        if not tracks:
            return None
        quiet = min((t.silent_for for t in tracks), default=0.0)
        # 이 세그먼트가 시작부터 내내 조용했으면 앞 세그먼트의 무음에 이어 붙임
        elapsed = time.time() - self._segment_t0 if self._segment_t0 else 0.0
        if self._quiet_carry and elapsed and quiet >= elapsed - 5.0:
            quiet += self._quiet_carry
        tail = ("녹화와 업로드는 계속합니다(무음으로 멈추지 않음). 장치가 맞는지 "
                "python -m syncnote devices 로 확인하세요.")
        if self.silence_abort_sec and quiet >= self.silence_abort_sec:
            return ("very_long", f"모든 트랙이 {quiet / 60:.0f}분째 무음입니다. {tail}")
        if self.silence_stop_sec and quiet >= self.silence_stop_sec:
            return ("long", f"모든 트랙이 {quiet / 60:.0f}분째 무음입니다. {tail}")
        return None

    def _carry_silence(self, audio_rec, ended_at: float) -> None:
        """세그먼트 끝까지 이어진 무음을 다음 세그먼트로 넘깁니다(무음 시간을 이어서 세려고)."""
        tracks = [t for t in getattr(audio_rec, "results", []) or [] if not t.error]
        if not tracks:
            self._quiet_carry = 0.0
            return
        quiet = min(t.silent_for for t in tracks)
        elapsed = ended_at - self._segment_t0 if self._segment_t0 else 0.0
        self._quiet_carry = (self._quiet_carry + quiet if quiet >= elapsed - 5.0 else quiet)

    def _note_silence(self, audio_rec, result, index: int) -> None:
        """긴 무음을 단계마다 한 번씩 경고합니다. 녹화를 멈추지 않습니다."""
        verdict = self._silence_verdict(audio_rec)
        if verdict is None:
            self._silence_warned = None            # 소리가 다시 나면 다음 무음을 새로 경고
            return
        if verdict[0] == self._silence_warned:
            return
        self._silence_warned = verdict[0]
        result.warnings.append(f"세그먼트 {index}: {verdict[1]}")
        log.warning("%s", verdict[1])

    def _invalidate_and_fallback_mic(self) -> object | None:
        """마이크 에러 발생 시 캐시를 무효화하고 살아있는 다른 마이크로 전환합니다.

        아예 입력 신호가 0이거나 고장 난 장치는 select_working_mic 의
        프로브 단계에서 dead 로 판정되어 굳이 배제하지 않아도 자동으로
        다음 살아있는 장치로 넘어갑니다.
        """
        self._mic_fallback_count += 1
        self._mic_source = _UNSET

        cfg = self.cfg
        explicit = str(cfg.get("recording.audio.mic_device", "") or "").strip()
        if explicit or not cfg.get("recording.audio.mic_auto_select", True):
            log.warning("mic_device가 고정되어 있거나 auto_select가 꺼져 있어 자동 전환을 수행하지 않습니다.")
            self._mic_source = None
            return None

        new_mic, probes = devices.select_working_mic(
            duration_sec=float(cfg.get("recording.audio.mic_probe_sec", 1.5)),
            samplerate=int(cfg.get("recording.audio.samplerate", 48000)),
            channels=int(cfg.get("recording.audio.channels", 2)),
        )
        self._log_mic_choice(new_mic, probes)
        if new_mic is None or getattr(new_mic, "dead", False):
            log.error("사용 가능한 마이크가 없습니다. 마이크 없이 계속 녹화합니다.")
            self._mic_source = None
            return None

        log.info("마이크 전환 완료: '%s'", getattr(new_mic, "name", "(기본)"))
        self._mic_source = new_mic
        return new_mic

    def _handle_audio_track_errors(self, audio_results: list, result: SessionResult) -> None:
        """오디오 트랙 결과 중 마이크 에러 발생 시 전환을 트리거하고 경고를 기록합니다."""
        for track in audio_results:
            if track.error:
                result.warnings.append(f"세그먼트 {track.name}: {track.error}")
                if track.name == "mic":
                    result.warnings.append(f"마이크 트랙 실패: {track.error}")
                    self._invalidate_and_fallback_mic()
            elif track.silent:
                result.warnings.append(
                    f"세그먼트 {track.name}: 전 구간 무음 (장치 설정 확인 필요)"
                )

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

        def pick():
            return devices.select_working_mic(
                duration_sec=float(cfg.get("recording.audio.mic_probe_sec", 1.5)),
                samplerate=int(cfg.get("recording.audio.samplerate", 48000)),
                channels=int(cfg.get("recording.audio.channels", 2)),
            )

        mic, probes = pick()
        # 전부 죽었고 Windows 마이크 권한이 실제로 꺼져 있으면: 알리고, 켜고, 다시 시험
        if probes and all(p.dead for p in probes) and devices.mic_permission_denied():
            log.warning("Windows 마이크 권한이 꺼져 있어 마이크가 녹음되지 않습니다. 켭니다.")
            changed, problems = devices.enable_mic_permission()
            for c in changed:
                log.warning("  켰습니다: %s", c)
            for p in problems:
                log.error("  %s", p)
            if changed:
                mic, probes = pick()
        self._log_mic_choice(mic, probes)
        self._guide_if_mix_only(probes)
        self._mic_source = mic
        return mic

    @staticmethod
    def _guide_if_mix_only(probes) -> None:
        """기본 녹음 장치가 스테레오 믹스이고 마이크 입력이 없으면 드라이버 안내(사람이 해야 함)."""
        try:
            sc = devices._sc()
            default = str(getattr(sc.default_microphone(), "name", "") or "")
            names = [str(getattr(m, "name", "") or "")
                     for m in sc.all_microphones(include_loopback=False)]
        except Exception:  # noqa: BLE001 — 안내는 녹화를 막지 않음
            return
        if devices.mix_only(default, names, probes):
            from . import driver_help

            for line in devices.driver_guidance(default, driver_help.baseboard()).splitlines():
                log.error("%s", line)

    @staticmethod
    def _log_mic_choice(mic, probes) -> None:
        if not probes:
            log.warning("실제 마이크가 없습니다(스테레오 믹스는 기본 마이크로 고르지 않고, 있으면 "
                        "따로 한 트랙으로 함께 녹음합니다).")
            return
        if all(p.dead for p in probes):
            # '열지 못함' 과 '열었는데 정확히 0' 은 고칠 곳이 다릅니다. 권한은 짐작하지 않고
            # 실제 설정을 읽어서만 말합니다(강의실 PC: 권한은 Allow 인데 라이브러리가 장치
            # 형식을 못 받아 열지 못한 것을 '권한이 꺼져 있을 가능성' 이라고 안내했음).
            for p in probes:
                log.error("마이크 '%s': %s", p.name,
                          f"열지 못했습니다 — {p.error}" if p.error else "열었지만 신호가 정확히 0")
            denied = devices.mic_permission_denied()
            if denied:
                log.error("Windows 설정 > 개인 정보 및 보안 > 마이크 > '데스크톱 앱이 마이크에 "
                          "액세스하도록 허용' 이 꺼져 있습니다. 켜야 마이크가 녹음됩니다.")
            elif any(p.peak == 0.0 and not p.error for p in probes):
                log.error("마이크 권한은 켜져 있습니다%s. 정확히 0 이면 가상 장치·음소거·"
                          "단자에 아무것도 연결되지 않은 경우입니다.",
                          "" if denied is False else "(확인 못 함)")
            log.error("그래도 '%s' 로 시도는 합니다.", getattr(mic, "name", "(없음)"))
            return
        chosen = probes[-1]
        if chosen.via:
            log.warning("마이크 '%s' 는 soundcard 가 열지 못해 %s 로 엽니다.",
                        chosen.name, chosen.via)
        if chosen.faint:
            log.warning("마이크 '%s' 의 소리가 아주 작습니다(최대레벨 %.5f, -60 dB 아래) — "
                        "단자에 마이크가 꽂혀 있지 않거나 입력 볼륨이 0 에 가깝습니다.",
                        chosen.name, chosen.peak)
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

        # ffmpeg 가 없으면 화면은 잡을 수 없지만 소리(soundcard -> WAV)는 됩니다.
        # 수업을 통째로 잃지 않게 소리만 녹음하고, 파일(mkv/mp4)은 ffmpeg 가 생긴 뒤
        # `record remux` 로 만듭니다. 세그먼트마다 실패를 반복하지 않게 처음에 한 번 봅니다.
        self.audio_only = False
        if audio_on:
            from .ffmpeg_util import FFmpegMissing, find_ffmpeg
            try:
                find_ffmpeg()
            except FFmpegMissing:
                self.audio_only = True
                video_on = False
                msg = ("ffmpeg 가 없어 소리만 녹음합니다(화면은 녹화되지 않음). "
                       "ffmpeg 를 설치한 뒤 `python -m syncnote record remux` 로 파일을 만드세요.")
                log.warning("%s", msg)
                result.warnings.append(msg)

        log.info("=" * 58)
        log.info("녹화 시작: %s", self.label)
        log.info("  폴더   : %s", out_dir)
        log.info("  세그먼트: %g분  /  최대: %g분", seg_minutes, max_minutes)
        log.info("  영상=%s  오디오=%s", "켬" if video_on else "끔", "켬" if audio_on else "끔")
        log.info("  Ctrl+C 또는 stop 명령으로 안전하게 중단할 수 있습니다.")
        log.info("=" * 58)

        deadline = time.time() + max_minutes * 60
        index = 0
        fails = 0

        while not self._stop_event.is_set() and time.time() < deadline:
            index += 1
            remaining_min = (deadline - time.time()) / 60
            this_len = min(seg_minutes, remaining_min)
            if this_len <= 0.05:
                break
            # 화면 ffmpeg 가 연달아 곧바로 죽으면 이번 세그먼트는 소리만(소리를 잃지 않게).
            # 다음 세그먼트에서 화면을 다시 시도합니다.
            video_this = video_on and self._video_quick_deaths < 2
            if video_on and not video_this:
                log.warning("화면 녹화가 연달아 곧바로 멈춰 이번 세그먼트는 소리만 녹음합니다"
                            "(다음 세그먼트에서 화면을 다시 시도).")
                self._video_quick_deaths = 0
            self._video_died = False
            seg_t0 = time.time()
            try:
                segment = self._record_segment(index, out_dir, this_len, container,
                                               video_this, audio_on, result)
                fails = 0
            except Exception as exc:
                # 한 세그먼트가 실패했다고 녹화 전체를 멈추지 않습니다 — 그러면 수업의 나머지가
                # 통째로 빕니다. 간격을 두고(5 → 15 → 30 → 60초) 다음 세그먼트로 다시 시도합니다.
                log.exception("세그먼트 %d 실패 — 잠시 뒤 다음 세그먼트로 계속합니다", index)
                result.warnings.append(f"세그먼트 {index} 실패(녹화는 계속): {exc}")
                wait = (5, 15, 30, 60)[min(fails, 3)]
                fails += 1
                self._stop_event.wait(min(wait, max(0.0, deadline - time.time())))
                continue
            if self._video_died:
                # 화면 ffmpeg 가 죽어 세그먼트를 일찍 마무리했음 — 세션은 계속(다음 세그먼트에서 다시 켬)
                quick = time.time() - seg_t0 < 60
                self._video_quick_deaths = self._video_quick_deaths + 1 if quick else 0
            else:
                self._video_quick_deaths = 0
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

    def _journal(self, work: Path, stem: str, index: int, container: str, started: datetime,
                 audio_rec, video_recs) -> None:
        from .recover import write_journal

        # 일지는 복구를 돕는 것일 뿐 — 무슨 일이 있어도 녹화를 멈추게 하지 않습니다.
        try:
            meta = getattr(self, "meta", None)
            tracks = getattr(audio_rec, "tracks", None)
            data = {
                "version": 1, "stem": stem, "index": index, "container": container,
                "layout": str(self.cfg.get("recording.audio.track_layout", "separate")),
                "label": self.label, "course_id": str(getattr(self.course, "id", "") or ""),
                "started": started.isoformat(),
                "audio": {str(t.track_name): float(t.result.started_at or 0)
                          for t in (tracks if isinstance(tracks, list) else [])},
                "video": {sfx: float(vr.started_at or 0) for vr, sfx in video_recs
                          if vr is not None},
                "meta": meta.to_state() if meta is not None else None,
            }
        except Exception as exc:
            log.warning("녹화 일지 내용을 만들지 못했습니다(녹화는 계속): %s", exc)
            return
        write_journal(work, stem, data)

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
                vad_detector=self._vad_detector,
                backend=str(cfg.get("recording.audio.backend", "soundcard")),
                stereo_mix_enabled=bool(cfg.get("recording.audio.stereo_mix_enabled", True)),
                stereo_mix_device=str(cfg.get("recording.audio.stereo_mix_device", "")),
            )
            audio_rec.start(stem)
        # 녹화 일지: 컴퓨터가 녹화 중에 꺼지면 다음 시작 때 이것으로 조각을 합쳐 올립니다.
        self._journal(work, stem, index, container, now, audio_rec, [])

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
        self._segment_t0 = time.time()
        self._journal(work, stem, index, container, now, audio_rec, video_recs)
        journal_again = time.time() + 5      # 소리 트랙 시작 시각은 첫 소리가 들어온 뒤 정해짐
        end_at = time.time() + minutes * 60
        log.info("세그먼트 %d 녹화 중 (%.1f분)", index, minutes)
        while time.time() < end_at and not self._stop_event.is_set():
            time.sleep(0.5)
            if journal_again and time.time() >= journal_again:
                self._journal(work, stem, index, container, now, audio_rec, video_recs)
                journal_again = 0.0
            # 화면 ffmpeg 가 죽으면 이 세그먼트만 마무리하고 녹화는 계속합니다(다음 세그먼트에서
            # 화면을 다시 켬). 예전에는 녹화 전체를 멈춰 수업의 나머지를 잃었습니다.
            for vr, sfx in video_recs:
                if vr is not None and not vr.is_running:
                    log.error("화면 ffmpeg 가 예기치 않게 종료됐습니다%s. 이 세그먼트를 마무리하고 "
                              "다음 세그먼트로 계속합니다.", f" (모니터{sfx})" if multi else "")
                    result.warnings.append(f"세그먼트 {index}: ffmpeg 비정상 종료{sfx}(녹화는 계속)")
                    self._video_died = True
                    break
            if self._stop_event.is_set() or self._video_died:
                break
            self._note_silence(audio_rec, result, index)    # 경고만 — 멈추지 않음

        # --- 정지 ---
        ended_at = time.time()
        self._carry_silence(audio_rec, ended_at)
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

        self._handle_audio_track_errors(audio_results, result)

        # --- 합치기: 모니터마다 독립 파일 ---
        mux_inputs = [
            MuxInput(t.path, t.name, t.started_at or audio_started)
            for t in audio_results if t.path.exists() and not t.error
        ]
        wav_copies = {t.name: t.path for t in audio_results if t.path.exists()}
        keep_wav = bool(cfg.get("transcribe.enabled", True))
        audio_only = bool(getattr(self, "audio_only", False)) and bool(mux_inputs)

        # 모니터가 여러 개면 모니터마다 mux, 하나면 기존대로.
        final_paths: list[Path] = []
        for video_path, sfx in ([] if audio_only else stopped_video_paths):
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
                    meta=self.meta, segment_index=index, ended_at=ended_at,
                    mic_gain_filter=autogain.from_config(cfg),
                )
                final_paths.append(final)
            except Exception as exc:
                log.error("합치기 실패%s: %s", f" (모니터{sfx})" if multi else "", exc)
                result.warnings.append(f"세그먼트 {index} 합치기 실패{sfx}: {exc}")

        # WAV 삭제 (전사 불필요) or 이동. 소리만 녹음했으면 그 WAV 가 유일한 기록이라
        # 전사 설정과 상관없이 남깁니다(파일을 만든 뒤 지울지는 pending 에 적어 둠).
        if not keep_wav and not audio_only:
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

        # 정상으로 마무리됨 — 일지가 남아 있으면 다음 시작 때 '꺼진 녹화' 로 봅니다
        from .recover import clear_journal
        clear_journal(work, stem)

        # 작업 폴더 정리
        try:
            leftover = list(work.iterdir())
            if leftover:
                log.debug("작업 폴더에 %d개 파일이 남았습니다: %s", len(leftover), work)
            else:
                work.rmdir()
        except OSError as exc:
            log.debug("작업 폴더 정리 생략: %s", exc)

        if audio_only:
            from .remux import write_pending
            write_pending(
                out_dir / f"{stem}.{container}",
                [(t.name, wav_copies.get(t.name, t.path), t.started_at or audio_started)
                 for t in audio_results if not t.error and wav_copies.get(t.name)],
                layout=str(cfg.get("recording.audio.track_layout", "separate")),
                meta=self.meta, segment_index=index, ended_at=ended_at,
                delete_wav=not keep_wav, course_id=str(getattr(self.course, "id", "") or ""),
                started=now)

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
