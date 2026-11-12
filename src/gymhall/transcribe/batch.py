"""녹화가 끝난 뒤 돌리는 정밀 전사. 이쪽이 본체입니다.

대기열(queue) 모드:
  저사양 노트북에서 녹화 직후 바로 전사를 돌리면 발열과 배터리
  소모가 심합니다. 대기열에 쌓아 두었다가 충전기에 꽂았을 때
  한 번에 돌리는 쪽이 현실적입니다.
    python -m gymhall transcribe queue
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get as get_logger
from ..paths import Layout
from . import formats
from .cleanup import Cleaner
from .cleanup import from_config as cleaner_from_config
from .engine import EngineSpec
from .language import describe as describe_language
from .language import parse as parse_language
from .language import restrict as restrict_language
from .language import whisper_argument
from .perf import Profile, describe_estimate, human_duration

log = get_logger("transcribe.batch")

QUEUE_FILE = "transcribe_queue.json"


# ---------------------------------------------------------------------------
# 대기열
# ---------------------------------------------------------------------------
class Queue:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("대기열 파일이 손상돼 새로 만듭니다: %s", self.path)
                self.items = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")

    def add(self, audio: Path, *, label: str, out_dir: Path, track: str = "system",
            language: str | None = None) -> None:
        entry = {
            "audio": str(audio),
            "label": label,
            "out_dir": str(out_dir),
            "track": track,
            "language": language,
            "queued_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        if any(i["audio"] == entry["audio"] for i in self.items):
            return
        self.items.append(entry)
        self.save()
        log.info("전사 대기열에 추가: %s", Path(audio).name)

    def pop_all(self) -> list[dict[str, Any]]:
        items, self.items = list(self.items), []
        self.save()
        return items

    def remove(self, audio: str) -> None:
        self.items = [i for i in self.items if i["audio"] != audio]
        self.save()

    def __len__(self) -> int:
        return len(self.items)

    def describe(self, profile: Profile | None = None,
                 model: str = "small", device: str = "cpu") -> str:
        if not self.items:
            return "전사 대기열이 비어 있습니다."
        lines = [f"전사 대기 {len(self.items)}건", "=" * 50]
        for item in self.items:
            path = Path(item["audio"])
            size = f"{path.stat().st_size/1024/1024:.0f} MB" if path.exists() else "파일 없음"
            lang = f" [{item['language']}]" if item.get("language") else ""
            lines.append(f"  [{item['track']}]{lang} {path.name}  ({size})")
        if profile is not None:
            total = _queue_audio_seconds(self.items)
            if total > 0:
                lines.append("")
                lines.append(f"  오디오 합계 {human_duration(total)}")
                lines.append(f"  {describe_estimate(profile, total, model, device)}")
        return "\n".join(lines)


PROFILE_FILE = "perf_profile.json"


def _queue_audio_seconds(items: list[dict[str, Any]]) -> float:
    """대기열에 쌓인 오디오의 총 길이. 못 재면 0."""
    from ..recorder.ffmpeg_util import duration_of

    total = 0.0
    for item in items:
        path = Path(item.get("audio", ""))
        if not path.exists():
            continue
        try:
            total += duration_of(path) or 0.0
        except Exception:
            continue
    return total


# ---------------------------------------------------------------------------
# 전사
# ---------------------------------------------------------------------------
@dataclass
class Result:
    audio: Path
    outputs: list[Path]
    seconds_audio: float
    seconds_spent: float
    language: str = ""

    @property
    def speed(self) -> float:
        return self.seconds_audio / self.seconds_spent if self.seconds_spent else 0.0


def transcribe_file(
    audio: Path,
    out_stem: Path,
    *,
    spec: EngineSpec,
    language: str = "ko",
    output_formats: tuple[str, ...] = ("txt", "srt"),
    vad_filter: bool = True,
    model=None,
    header: str = "",
    cleaner: Cleaner | None = None,
    perf: Profile | None = None,
    device: str = "cpu",
) -> Result:
    """오디오 하나를 전사해 결과 파일들을 씁니다."""
    audio = Path(audio)
    if not audio.exists():
        raise FileNotFoundError(audio)

    model = model or spec.load()
    started = time.time()
    log.info("전사 시작: %s", audio.name)

    candidates = parse_language(language)
    log.info("  언어: %s", describe_language(candidates))
    segments_iter, info = model.transcribe(
        str(audio),
        language=whisper_argument(candidates),
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 800} if vad_filter else None,
        beam_size=5,
        word_timestamps="json" in output_formats,
        condition_on_previous_text=True,
    )

    cleaner = cleaner or Cleaner()
    segments: list[formats.Segment] = []
    last_report = time.time()
    for seg in segments_iter:
        drop, reason = cleaner.should_drop(
            seg.text,
            getattr(seg, "no_speech_prob", None),
            getattr(seg, "avg_logprob", None),
        )
        if drop:
            log.debug("[%.0fs] 제거(%s): %s", seg.start, reason, seg.text.strip()[:50])
            continue
        words = []
        if getattr(seg, "words", None):
            words = [{"start": w.start, "end": w.end, "word": w.word} for w in seg.words]
        segments.append(formats.Segment(seg.start, seg.end, seg.text.strip(), words))
        # 긴 파일에서 멈춘 것처럼 보이지 않도록 진행 상황을 찍습니다.
        if time.time() - last_report > 30:
            log.info("  ... %.0f분 지점까지 전사", seg.end / 60)
            last_report = time.time()
    cleaner.report(audio.name)

    if perf is not None:
        perf.record(
            float(getattr(info, "duration", 0.0) or 0.0),
            time.time() - started, spec.model, device,
        )

    spent = time.time() - started
    audio_seconds = float(getattr(info, "duration", 0.0) or 0.0)
    detected = restrict_language(info, candidates)

    out_stem = Path(out_stem)
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "source": str(audio),
        "model": spec.model,
        "language": detected.code,
        "language_probability": round(detected.probability, 3),
        "language_candidates": list(candidates),
        "audio_seconds": audio_seconds,
        "transcribed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(spent, 1),
        "cleanup": {
            "kept": cleaner.stats.kept,
            "dropped": cleaner.stats.dropped,
            "dropped_phrase": cleaner.stats.dropped_phrase,
            "dropped_silence": cleaner.stats.dropped_silence,
            "dropped_repeat": cleaner.stats.dropped_repeat,
        },
    }
    outputs = formats.write_all(out_stem, segments, output_formats, header=header, meta=meta)

    log.info(
        "전사 완료: %s  (음성 %.1f분 / 소요 %.1f분 / 배속 %.1fx)",
        audio.name, audio_seconds / 60, spent / 60,
        audio_seconds / spent if spent else 0,
    )
    return Result(audio, outputs, audio_seconds, spent, detected.code)


def run_queue(cfg, layout: Layout, *, limit: int = 0,
              language: str | None = None) -> list[Result]:
    """대기열을 비웁니다. 모델은 한 번만 로드해 재사용합니다."""
    queue = Queue(layout.state / QUEUE_FILE)
    if not len(queue):
        log.info("전사 대기열이 비어 있습니다.")
        return []

    spec = EngineSpec(
        model=str(cfg.get("transcribe.batch.model", "small")),
        device=str(cfg.get("transcribe.batch.device", "auto")),
        compute_type=str(cfg.get("transcribe.batch.compute_type", "int8")),
        cpu_threads=int(cfg.get("transcribe.batch.cpu_threads", 0)),
    )
    output_formats = tuple(cfg.get("transcribe.formats", ["txt", "srt"]))
    default_language = language or str(cfg.get("transcribe.language", "ko"))
    vad = bool(cfg.get("transcribe.batch.vad_filter", True))

    from .engine import pick_device

    device, _compute = pick_device(spec.device)
    perf = Profile(layout.state / PROFILE_FILE)

    results: list[Result] = []
    items = list(queue.items)
    if limit:
        items = items[:limit]

    # 시작하기 전에 전체가 얼마나 걸릴지 알려 줍니다. 저사양 노트북에서
    # 3시간짜리를 medium 으로 돌리면 몇 시간이 걸리는데, 그걸 모르고
    # 시작하면 배터리로 나갔다가 중간에 꺼집니다.
    total_audio = _queue_audio_seconds(items)
    if total_audio > 0:
        log.info(
            "전사 대기 %d건, 오디오 %s — %s",
            len(items), human_duration(total_audio),
            describe_estimate(perf, total_audio, spec.model, device),
        )

    model = spec.load()

    for item in items:
        audio = Path(item["audio"])
        if not audio.exists():
            log.warning("파일이 사라져 대기열에서 제거합니다: %s", audio)
            queue.remove(item["audio"])
            continue
        out_dir = Path(item["out_dir"])
        stem = out_dir / f"{audio.stem}"
        header = f"{item.get('label','')} / 트랙: {item.get('track','')}"
        # 과목마다 언어가 다를 수 있습니다(영어 수업 등).
        # 대기열에 적힌 값이 있으면 전역 설정보다 우선합니다.
        item_language = language or item.get("language") or default_language
        try:
            results.append(
                transcribe_file(
                    audio, stem, spec=spec, language=item_language,
                    output_formats=output_formats, vad_filter=vad,
                    model=model, header=header,
                    # 파일마다 새로 만듭니다. 하나를 돌려쓰면 앞 파일의
                    # 마지막 문장이 다음 파일의 첫 문장과 '반복' 으로 묶입니다.
                    cleaner=cleaner_from_config(cfg),
                    perf=perf, device=device,
                )
            )
            queue.remove(item["audio"])
        except Exception as exc:
            log.error("전사 실패(대기열에 남겨 둡니다): %s -> %s", audio.name, exc)

    return results


def enqueue_segment(cfg, layout: Layout, segment, label: str,
                    language: str | None = None) -> None:
    """녹화 세그먼트의 오디오를 대기열에 넣거나 바로 전사합니다."""
    if not cfg.get("transcribe.enabled", True) or not cfg.get("transcribe.batch.enabled", True):
        return

    want = str(cfg.get("transcribe.source_track", "system"))
    tracks = segment.wav_paths or {}
    targets = [(n, p) for n, p in tracks.items() if want == "both" or n == want]
    if not targets:
        log.debug("전사할 오디오 트랙이 없습니다(원하는 트랙: %s).", want)
        return

    out_dir = layout.transcript_dir(segment.started_at, label)
    queue = Queue(layout.state / QUEUE_FILE)
    for name, path in targets:
        queue.add(Path(path), label=label, out_dir=out_dir, track=name,
                  language=language)

    if str(cfg.get("transcribe.batch.mode", "queue")) == "immediate":
        log.info("즉시 전사 모드입니다.")
        run_queue(cfg, layout)
