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
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger
from .engine import EngineSpec

log = get_logger("transcribe.realtime")


class RealtimeTranscriber(threading.Thread):
    """오디오 큐를 받아 주기적으로 전사해 파일에 덧붙입니다.

    @dataclass 를 쓰지 않는 이유:
      dataclass 는 __eq__ 를 만들면서 __hash__ 를 None 으로 지웁니다.
      threading.Thread.__init__ 은 내부 WeakSet 에 self 를 넣기 때문에
      해시가 가능해야 하고, 그래서 dataclass 로 만든 Thread 하위 클래스는
      '생성 자체' 가 TypeError: unhashable type 으로 실패합니다.
      (eq=False 를 줘도 되지만, 평범한 __init__ 이 더 명확합니다.)
    """

    def __init__(
        self,
        out_path: Path,
        spec: EngineSpec,
        *,
        samplerate: int = 48000,
        chunk_sec: float = 8.0,
        overlap_sec: float = 1.5,
        language: str = "ko",
        print_to_console: bool = True,
        max_queue: int = 64,
    ) -> None:
        super().__init__(name="realtime-stt", daemon=True)
        self.out_path = Path(out_path)
        self.spec = spec
        self.samplerate = int(samplerate)
        self.chunk_sec = float(chunk_sec)
        self.overlap_sec = min(float(overlap_sec), float(chunk_sec) * 0.5)
        self.language = language
        self.print_to_console = bool(print_to_console)
        self.audio_queue: queue.Queue = queue.Queue(maxsize=int(max_queue))
        self.lines: list[str] = []
        # 이름을 `_stop` 으로 바꾸면 안 됩니다. threading.Thread 가 3.12 까지
        # 내부 메서드 `_stop()` 을 가지고 있어 join() 이 TypeError 로 터집니다.
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    # -----------------------------------------------------------------
    @staticmethod
    def _to_mono(block):
        """스테레오 블록을 모노로 평균합니다.

        반환형에 numpy 타입을 쓰지 않는 이유: numpy 를 모듈 수준에서
        임포트하지 않습니다. 실시간 전사를 끈 사용자가 numpy 없이도
        이 모듈을 불러올 수 있어야 합니다.
        """
        return block.mean(axis=1) if block.ndim > 1 else block

    def _resample_to_16k(self, chunk):
        """Whisper 는 16kHz 모노를 받습니다.

        chunk[::3] 같은 단순 추출(데시메이션)은 8kHz 위의 성분을 되접어
        넣어(에일리어싱) 자음을 뭉갭니다. 정수배일 때는 평균을 내 간단한
        저역통과를 겸하고, 정수배가 아니면 선형보간으로 맞춥니다.
        """
        import numpy as np

        target = 16000
        if self.samplerate == target:
            return chunk.astype(np.float32)

        if self.samplerate % target == 0:
            factor = self.samplerate // target
            usable = (len(chunk) // factor) * factor
            if usable == 0:
                return chunk.astype(np.float32)
            return chunk[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)

        out_len = max(1, int(round(len(chunk) * target / self.samplerate)))
        src_x = np.arange(len(chunk), dtype=np.float64)
        dst_x = np.linspace(0, len(chunk) - 1, out_len)
        return np.interp(dst_x, src_x, chunk).astype(np.float32)

    def _transcribe_chunk(self, model, chunk) -> str:
        """조각 하나를 전사해 텍스트를 돌려줍니다. 실패하면 빈 문자열."""
        audio16 = self._resample_to_16k(chunk)
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
            return ""

        spent = time.time() - started
        if spent > self.chunk_sec:
            self._behind += 1
            if self._behind in (1, 5, 20):
                log.warning(
                    "실시간 전사가 입력 속도를 따라가지 못합니다 "
                    "(%.1f초 분량 처리에 %.1f초). 모델을 더 작게 하거나 끄세요.",
                    self.chunk_sec, spent,
                )
        return text

    def _emit(self, handle, text: str) -> None:
        stamp = f"[{int(self._elapsed // 60):02d}:{int(self._elapsed % 60):02d}]"
        line = f"{stamp} {text}"
        self.lines.append(line)
        try:
            handle.write(line + "\n")
            handle.flush()
        except (OSError, ValueError):
            pass
        if self.print_to_console:
            print(line, flush=True)

    def _drain(self, buffer, model, handle, *, final: bool):
        """처리할 수 있는 조각을 모두 전사하고 남은 버퍼를 돌려줍니다.

        final=True 는 정지 신호를 받은 마지막 호출입니다. 이때는 조각이
        chunk_sec 보다 짧아도(1초 이상이면) 전사합니다. 그렇지 않으면
        강의 마지막 몇 초가 통째로 사라집니다.
        """
        target = self._target_frames
        minimum = target if not final else self.samplerate  # 1초
        while len(buffer) >= minimum:
            chunk = buffer[:target]
            buffer = buffer[min(self._advance, len(buffer)):]
            text = self._transcribe_chunk(model, chunk)
            if text:
                self._emit(handle, text)
            self._elapsed += self._advance / self.samplerate
            if final and len(buffer) < minimum:
                break
        return buffer

    def run(self) -> None:
        import numpy as np

        try:
            model = self.spec.load()
        except Exception as exc:
            log.error("실시간 전사 모델을 불러오지 못해 비활성화합니다: %s", exc)
            return

        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.out_path.open("a", encoding="utf-8")
        except OSError as exc:
            log.error("실시간 전사 파일을 열 수 없습니다: %s", exc)
            return

        handle.write(f"\n# 실시간 임시 전사 — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        handle.write("# 이 파일은 임시본입니다. 녹화 종료 후 정밀 전사본을 보세요.\n\n")
        handle.flush()

        self._target_frames = max(1, int(self.chunk_sec * self.samplerate))
        self._advance = max(1, self._target_frames - int(self.overlap_sec * self.samplerate))
        self._elapsed = 0.0
        self._behind = 0
        buffer = np.zeros((0,), dtype=np.float32)

        try:
            while True:
                try:
                    block = self.audio_queue.get(timeout=0.25)
                except queue.Empty:
                    if self._stop_event.is_set():
                        # 큐가 비었고 정지 신호가 왔습니다. 남은 꼬리를 처리합니다.
                        self._drain(buffer, model, handle, final=True)
                        break
                    continue

                buffer = np.concatenate([buffer, self._to_mono(block).astype(np.float32)])
                buffer = self._drain(buffer, model, handle, final=False)

        except Exception:
            log.exception("실시간 전사 루프 오류")
        finally:
            try:
                handle.write("\n")
                handle.close()
            except (OSError, ValueError):
                pass
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
