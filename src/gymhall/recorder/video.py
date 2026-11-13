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
from .ffmpeg_util import find_ffmpeg, popen, read_stderr_log
from .monitors import enumerate_monitors, pick

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
    # 0 = 전체 바탕화면(지금까지의 동작). 1 부터는 그 화면만.
    # window_title 이 있으면 이 값은 무시합니다 — 창 하나가 화면
    # 하나보다 좁으니 더 좁은 쪽이 이깁니다.
    monitor: int = 0
    draw_mouse: bool = True
    container: str = "mkv"

    _proc: subprocess.Popen | None = None
    _path: Path | None = None
    _err_log: Path | None = None
    _started_at: float = 0.0

    # -- 명령 구성 ---------------------------------------------------
    def _input_args(self) -> list[str]:
        if os.name != "nt":
            display = os.environ.get("DISPLAY", ":0.0")
            return ["-f", "x11grab", "-framerate", str(self.fps), "-i", display]
        return self.win_input_args(enumerate_monitors())

    def win_input_args(self, monitors: list) -> list[str]:
        """Windows 입력 인자. 화면 목록을 **인자로 받습니다.**

        직접 enumerate_monitors() 를 부르면 리눅스 CI 에서 이 함수가
        한 줄도 안 돕니다 — _input_args 가 그 위에서 x11grab 으로
        빠지기 때문입니다. 그러면 모니터 선택 전체가 "테스트는 초록,
        코드는 한 번도 안 돌아봄" 이 됩니다(docs/TESTING.md 참고).
        목록을 밖에서 받으면 어느 OS 에서나 시험할 수 있습니다.
        """
        # window_title 이 우선입니다. 창 하나는 화면 하나보다 좁고,
        # 좁을수록 엉뚱한 것이 덜 찍힙니다.
        if self.window_title:
            if self.backend == "ddagrab":
                log.warning("ddagrab 은 창 단위 캡처를 못 합니다. "
                            "window_title 을 쓰려면 backend=gdigrab 으로 바꾸세요.")
            else:
                return ["-f", "gdigrab", "-framerate", str(self.fps),
                        "-draw_mouse", "1" if self.draw_mouse else "0",
                        "-i", f"title={self.window_title}"]

        target = pick(monitors, self.monitor) if self.monitor else None

        if self.backend == "ddagrab":
            # ddagrab 는 필터 소스라 입력 지정 방식이 다릅니다.
            # 화면 선택은 잘라내기가 아니라 output_idx(0부터)로 합니다.
            src = f"ddagrab=framerate={self.fps}"
            if target is not None:
                # 주의: 이 번호가 맞는다는 보장이 없습니다.
                #
                # monitors.primary_first() 는 "주 모니터 먼저, 그다음
                # x 좌표순" 으로 번호를 다시 매깁니다. 사람이 세는
                # 순서와 맞추려는 것입니다. 그런데 ddagrab 의
                # output_idx 는 **그래픽 드라이버가 정한 D3D11 출력
                # 순서**입니다. 둘이 같다는 근거가 없습니다.
                #
                # 모니터가 하나면 둘 다 0 이라 차이가 안 납니다. 그래서
                # 1대 PC 에서는 원리적으로 검증할 수 없습니다. 2대
                # 실기가 필요합니다(docs/TESTING.md 의 테스트 불가 목록).
                #
                # 기본 백엔드인 gdigrab 은 좌표로 잘라내므로 이 문제가
                # 없습니다.
                src += f":output_idx={target.index - 1}"
                if len(monitors) > 1:
                    # 쓴 값을 그대로 적습니다. 그래야 2대 PC 에서
                    # monitor=1,2 를 차례로 시험해 "어느 idx 가 어느
                    # 화면인지" 를 사용자가 역으로 알아낼 수 있습니다.
                    # "지원 안 함" 이 아니라 "스스로 알아낼 수 있음" 이
                    # 됩니다.
                    log.warning(
                        "ddagrab + monitor=%d: output_idx=%d 로 녹화합니다. "
                        "출력 번호는 그래픽 드라이버가 정한 순서라 다른 "
                        "화면일 수 있습니다. 의심되면 backend = \"gdigrab\" "
                        "(기본값)을 쓰세요 — 그쪽은 좌표로 잘라내므로 "
                        "정확합니다.",
                        self.monitor, target.index - 1,
                    )
            else:
                # monitor=0 의 뜻이 두 백엔드에서 다릅니다.
                #   gdigrab -i desktop  = 전체 '가상' 바탕화면(모든 화면)
                #   ddagrab (idx 없음)  = 출력 0번, 보통 주 모니터 하나
                # 기본 백엔드가 gdigrab 이라 대부분은 안 겪지만,
                # ddagrab 을 켠 사람은 "왜 보조 모니터가 안 찍히지" 에서
                # 헤맵니다. 설정이 아니라 로그로 알려 줍니다.
                log.info("ddagrab 은 monitor=0 일 때 주 모니터 하나만 잡습니다"
                         "(gdigrab 의 desktop 은 전체 화면을 이어 붙입니다). "
                         "보조 모니터를 찍으려면 monitor 번호를 적으세요.")
            return ["-f", "lavfi", "-i", src]

        args = ["-f", "gdigrab", "-framerate", str(self.fps),
                "-draw_mouse", "1" if self.draw_mouse else "0"]
        if target is not None:
            # gdigrab 은 가상 바탕화면 좌표계에서 잘라냅니다. 보조
            # 모니터가 주 모니터 왼쪽에 있으면 x 가 음수입니다 —
            # 정상이고, 그대로 넘겨야 맞습니다.
            log.info("화면 %s 만 잡습니다.", target.describe())
            args += ["-offset_x", str(target.x), "-offset_y", str(target.y),
                     "-video_size", target.size]
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
        # stderr 는 파이프가 아니라 파일로 받습니다. 파이프로 받고 읽지
        # 않으면 버퍼가 차는 순간 ffmpeg 가 멈춰 섭니다(ffmpeg_util.popen 참고).
        self._err_log = path.with_suffix(path.suffix + ".ffmpeg.log")
        self._proc = popen(cmd, stderr_log=self._err_log)
        self._path = path
        self._started_at = time.time()

        # 즉시 죽는 경우(장치 없음, 창 제목 오타)를 바로 잡아냅니다.
        time.sleep(1.5)
        if self._proc.poll() is not None:
            tail = read_stderr_log(self._err_log)
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

        handle = getattr(self._proc, "_gymhall_stderr", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        for line in read_stderr_log(getattr(self, "_err_log", None), lines=5).splitlines():
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
