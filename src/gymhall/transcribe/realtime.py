"""실시간(근사) 전사.

솔직한 경고:
  저사양 노트북에서 화면 녹화와 동시에 돌리면 둘 다 나빠집니다.
  여기 나오는 결과는 '임시본' 입니다. 녹화가 끝난 뒤 batch 가
  훨씬 정확한 결과로 덮어씁니다.

왜 정확도가 떨어지나:
  고정 길이(기본 8초)로 잘라 넣기 때문에 문장 중간에서 끊깁니다.
  Whisper 는 앞뒤 문맥으로 단어를 고르는데 그 문맥이 없습니다.
  overlap_sec 으로 완화는 되지만 없애지는 못합니다.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger
from .engine import EngineSpec

log = get_logger("transcribe.realtime")


@dataclass
class RealtimeTranscriber(threading.Thread):
    """오디오 큐를 받아 주기적으로 전사해 파일에 덧붙입니다."""

    out_path: Path
    spec: EngineSpec
    samplerate: int = 48000
    chunk_sec: float = 8.0
    overlap_sec: float = 1.5
    language: str = "ko"
    print_to_console: bool = True
    max_queue: int = 64

    audio_queue: "queue.Queue" = field(default_factory=lambda: queue.Queue(maxsize=64))
    _stop: threading.Event = field(default_factory=threading.Event)
    lines: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        threading.Thread.__init__(self, name="realtime-stt", daemon=True)
        self.audio_queue = queue.Queue(maxsize=self.max_queue)
        self.out_path = Path(self.out_path)

    def stop(self) -> None:
        self._stop.set()

    # -----------------------------------------------------------------
    def run(self) -> None:
        import numpy as np

        try:
            model = self.spec.load()
        except Exception as exc:
            log.error("실시간 전사 모델을 불러오지 못해 비활성화합니다: %s", exc)
            return

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.out_path.open("a", encoding="utf-8")
        handle.write(f"\n# 실시간 임시 전사 — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        handle.write("# 이 파일은 임시본입니다. 녹화 종료 후 정밀 전사본을 보세요.\n\n")
        handle.flush()

        target = int(self.chunk_sec * self.samplerate)
        overlap = int(self.overlap_sec * self.samplerate)
        buffer = np.zeros((0,), dtype=np.float32)
        elapsed = 0.0
        dropped = 0

        try:
            while not self._stop.is_set() or not self.audio_queue.empty():
                try:
                    block = self.audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # 스테레오 -> 모노 (Whisper 는 16kHz 모노를 받습니다)
                mono = block.mean(axis=1) if block.ndim > 1 else block
                buffer = np.concatenate([buffer, mono.astype(np.float32)])

                if len(buffer) < target:
                    continue

                chunk = buffer[:target]
                buffer = buffer[max(0, target - overlap):]

                # 48k -> 16k 다운샘플 (단순 데시메이션. 음성에는 충분합니다)
                step = max(1, self.samplerate // 16000)
                audio16 = chunk[::step]

                started = time.time()
                try:
                    segments, _ = model.transcribe(
                        audio16,
                        language=None if self.language in ("", "auto") else self.language,
                        beam_size=1,              # 속도 우선
                        vad_filter=True,
                        condition_on_previous_text=False,
                    )
                    text = " ".join(s.text.strip() for s in segments).strip()
                except Exception as exc:
                    log.debug("실시간 전사 조각 실패: %s", exc)
                    continue

                spent = time.time() - started
                if spent > self.chunk_sec:
                    dropped += 1
                    if dropped in (1, 5, 20):
                        log.warning(
                            "실시간 전사가 입력 속도를 따라가지 못합니다 "
                            "(%.1f초 분량 처리에 %.1f초). 모델을 더 작게 하거나 끄세요.",
                            self.chunk_sec, spent,
                        )

                if text:
                    stamp = f"[{int(elapsed//60):02d}:{int(elapsed%60):02d}]"
                    line = f"{stamp} {text}"
                    self.lines.append(line)
                    handle.write(line + "\n")
                    handle.flush()
                    if self.print_to_console:
                        print(line, flush=True)

                elapsed += self.chunk_sec - self.overlap_sec

        except Exception:
            log.exception("실시간 전사 루프 오류")
        finally:
            handle.write("\n")
            handle.close()
            log.info("실시간 전사 종료: %d줄 -> %s", len(self.lines), self.out_path.name)


def maybe_start(cfg, out_dir: Path, label: str) -> RealtimeTranscriber | None:
    """설정이 켜져 있으면 실시간 전사기를 만들어 시작합니다."""
    if not cfg.get("transcribe.enabled", True):
        return None
    if not cfg.get("transcribe.realtime.enabled", False):
        return None

    spec = EngineSpec(
        model=str(cfg.get("transcribe.realtime.model", "tiny")),
        device=str(cfg.get("transcribe.realtime.device", "cpu")),
        compute_type=str(cfg.get("transcribe.realtime.compute_type", "int8")),
        cpu_threads=int(cfg.get("transcribe.realtime.cpu_threads", 2)),
    )
    worker = RealtimeTranscriber(
        out_path=Path(out_dir) / f"{label}_realtime.txt",
        spec=spec,
        samplerate=int(cfg.get("recording.audio.samplerate", 48000)),
        chunk_sec=float(cfg.get("transcribe.realtime.chunk_sec", 8)),
        overlap_sec=float(cfg.get("transcribe.realtime.overlap_sec", 1.5)),
        language=str(cfg.get("transcribe.language", "ko")),
        print_to_console=bool(cfg.get("transcribe.realtime.print_to_console", True)),
    )
    log.warning(
        "실시간 전사를 시작합니다(모델=%s). 녹화 품질이 떨어지면 "
        "config 의 transcribe.realtime.enabled 를 false 로 바꾸세요.",
        spec.model,
    )
    worker.start()
    return worker
