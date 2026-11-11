"""화면 캡처 (ffmpeg).

Windows:
  gdigrab  - 어디서나 동작. 저사양에 적합. 기본값.
  ddagrab  - Desktop Duplication API. 빠르지만 ffmpeg 빌드와 GPU 를 탑니다.
Linux (개발/테스트용):
  x11grab
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get as get_logger
from .ffmpeg_util import find_ffmpeg, popen

log = get_logger("recorder.video")


@dataclass
class VideoRecorder:
    out_dir: Path
    backend: str = "gdigrab"
    fps: int = 10
    scale_width: int = 1280
    scale_height: int = 0
    preset: str = "veryfast"
    crf: int = 30
    window_title: str = ""
    draw_mouse: bool = True
    container: str = "mkv"

    _proc: subprocess.Popen | None = None
    _path: Path | None = None
    _started_at: float = 0.0

    # -- 명령 구성 ---------------------------------------------------
    def _input_args(self) -> list[str]:
        if os.name != "nt":
            display = os.environ.get("DISPLAY", ":0.0")
            return ["-f", "x11grab", "-framerate", str(self.fps), "-i", display]

        if self.backend == "ddagrab":
            # ddagrab 는 필터 소스라 입력 지정 방식이 다릅니다.
            return ["-f", "lavfi", "-i", f"ddagrab=framerate={self.fps}"]

        args = ["-f", "gdigrab", "-framerate", str(self.fps),
                "-draw_mouse", "1" if self.draw_mouse else "0"]
        if self.window_title:
            args += ["-i", f"title={self.window_title}"]
        else:
            args += ["-i", "desktop"]
        return args

    def _filter(self) -> list[str]:
        filters = []
        if self.scale_width or self.scale_height:
            w = self.scale_width or -2
            h = self.scale_height or -2
            # -2 로 두면 비율을 유지하면서 짝수로 맞춥니다(h264 요구사항)
            filters.append(f"scale={w}:{h}:flags=bicubic")
        # 홀수 해상도 대비
        filters.append("pad=ceil(iw/2)*2:ceil(ih/2)*2")
        return ["-vf", ",".join(filters)] if filters else []

    def build_command(self, out_path: Path) -> list[str]:
        return [
            find_ffmpeg(),
            # -nostdin 을 쓰면 안 됩니다. 종료할 때 stdin 으로 'q' 를 보내
            # ffmpeg 가 파일을 정상적으로 마무리하게 하는데, 그 플래그가
            # 있으면 'q' 를 읽지 않아 강제 종료로 끝나고 끝부분이 잘립니다.
            "-hide_banner", "-loglevel", "warning", "-y",
            *self._input_args(),
            *self._filter(),
            "-c:v", "libx264",
            "-preset", self.preset,
            "-crf", str(self.crf),
            "-pix_fmt", "yuv420p",
            # 중간에 끊겨도 앞부분이 재생되도록 키프레임을 촘촘히
            "-g", str(max(1, self.fps * 4)),
            "-an",  # 오디오는 soundcard 가 따로 받습니다
            str(out_path),
        ]

    # -- 수명주기 ----------------------------------------------------
    def start(self, stem: str) -> Path:
        out_dir = Path(self.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{stem}.video.{self.container}"
        cmd = self.build_command(path)
        log.info("화면 녹화 시작 (%s, %dfps) -> %s", self.backend, self.fps, path.name)
        self._proc = popen(cmd)
        self._path = path
        self._started_at = time.time()

        # 즉시 죽는 경우(장치 없음, 창 제목 오타)를 바로 잡아냅니다.
        time.sleep(1.5)
        if self._proc.poll() is not None:
            stderr = (self._proc.stderr.read() or b"").decode("utf-8", "replace")
            tail = "\n".join(stderr.strip().splitlines()[-10:])
            self._proc = None
            raise RuntimeError(f"ffmpeg 가 바로 종료됐습니다:\n{tail}")
        return path

    def stop(self, timeout: float = 20.0) -> Path | None:
        """'q' 를 보내 ffmpeg 가 파일을 정상적으로 마무리하게 합니다.

        kill 하면 mp4 는 깨집니다. mkv 는 살아남지만 그래도 정상 종료가 낫습니다.
        """
        if self._proc is None:
            return self._path

        log.info("화면 녹화 종료 신호 전송")
        try:
            if self._proc.stdin:
                self._proc.stdin.write(b"q")
                self._proc.stdin.flush()
        except (OSError, ValueError):
            pass

        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg 가 %.0f초 안에 끝나지 않아 종료 신호를 보냅니다.", timeout)
            try:
                self._proc.send_signal(signal.SIGTERM if os.name != "nt" else signal.SIGTERM)
                self._proc.wait(timeout=10)
            except Exception:
                log.error("ffmpeg 강제 종료. 영상 끝부분이 잘릴 수 있습니다.")
                self._proc.kill()

        if self._proc.stderr:
            err = (self._proc.stderr.read() or b"").decode("utf-8", "replace").strip()
            if err:
                for line in err.splitlines()[-5:]:
                    log.debug("ffmpeg: %s", line)

        self._proc = None
        if self._path and self._path.exists():
            size = self._path.stat().st_size
            elapsed = max(1.0, time.time() - self._started_at)
            log.info("영상 저장: %s (%.1f MB, %.0f초)", self._path.name, size / 1024 / 1024, elapsed)
            # 완전히 정지된 화면은 정상적으로도 아주 작게 나옵니다.
            # 그래서 '아예 비어 있는' 경우만 경고합니다.
            if size < 2048:
                log.warning(
                    "영상 파일이 사실상 비어 있습니다(%d바이트). 캡처가 실패했을 수 있습니다.", size
                )
        return self._path

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def started_at(self) -> float:
        return self._started_at
