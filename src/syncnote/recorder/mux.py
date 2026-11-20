"""영상 + 오디오 트랙을 하나의 파일로 합칩니다.

영상과 오디오를 서로 다른 프로세스/스레드가 받으므로 시작 시각이
조금씩 다릅니다(보통 0.1~1.5초). 각자의 시작 타임스탬프를 기록해
두었다가 -itsoffset 으로 보정합니다. 이걸 안 하면 1시간 뒤에도
입모양과 소리가 어긋난 채로 남습니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get as get_logger
from .ffmpeg_util import find_ffmpeg, run

log = get_logger("recorder.mux")

TRACK_TITLES = {
    "system": "시스템 소리",
    "mic": "마이크",
    "mix": "합친 소리",
}


@dataclass
class MuxInput:
    path: Path
    name: str          # "system" | "mic"
    started_at: float  # time.time() 기준


def mux(
    out_path: Path,
    video: Path | None,
    video_started_at: float,
    audio: list[MuxInput],
    *,
    layout: str = "separate",
    audio_bitrate: str = "128k",
    delete_video: bool = True,
    delete_audio: bool = True,
) -> Path:
    """최종 파일을 만듭니다.

    layout:
      separate - 오디오 트랙을 각각 따로
      mixed    - 하나로 합침
      both     - 각각 + 합친 것
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    audio = [a for a in audio if a.path.exists() and a.path.stat().st_size > 44]
    if video is None and not audio:
        raise RuntimeError("합칠 영상도 오디오도 없습니다.")

    # 가장 먼저 시작한 것을 기준점으로 삼습니다.
    starts = [t for t in ([video_started_at] if video else []) + [a.started_at for a in audio] if t]
    base = min(starts) if starts else 0.0

    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "warning", "-y"]
    index = 0
    video_index = None
    audio_indices: list[tuple[int, str]] = []

    if video is not None and Path(video).exists():
        offset = max(0.0, video_started_at - base)
        if offset > 0.01:
            cmd += ["-itsoffset", f"{offset:.3f}"]
        cmd += ["-i", str(video)]
        video_index = index
        index += 1

    for track in audio:
        offset = max(0.0, track.started_at - base)
        if offset > 0.01:
            cmd += ["-itsoffset", f"{offset:.3f}"]
        cmd += ["-i", str(track.path)]
        audio_indices.append((index, track.name))
        index += 1

    want_mix = layout in {"mixed", "both"} and len(audio_indices) >= 2
    want_separate = layout in {"separate", "both"} or len(audio_indices) < 2

    if want_mix:
        inputs = "".join(f"[{i}:a]" for i, _ in audio_indices)
        cmd += ["-filter_complex",
                f"{inputs}amix=inputs={len(audio_indices)}:duration=longest:normalize=0[mixed]"]

    if video_index is not None:
        cmd += ["-map", f"{video_index}:v"]

    out_track_names: list[str] = []
    if want_separate:
        for i, name in audio_indices:
            cmd += ["-map", f"{i}:a"]
            out_track_names.append(name)
    if want_mix:
        cmd += ["-map", "[mixed]"]
        out_track_names.append("mix")

    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate]

    for position, name in enumerate(out_track_names):
        title = TRACK_TITLES.get(name, name)
        cmd += [f"-metadata:s:a:{position}", f"title={title}",
                f"-metadata:s:a:{position}", "language=kor"]

    cmd += ["-fflags", "+genpts", str(out_path)]

    log.info("합치는 중 -> %s (트랙: %s)", out_path.name, ", ".join(out_track_names) or "없음")
    run(cmd, timeout=3600)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(f"합치기 결과 파일이 비어 있습니다: {out_path}")

    log.info("완료: %s (%.1f MB)", out_path.name, out_path.stat().st_size / 1024 / 1024)

    # 영상 원본은 합친 파일에 그대로 복사(-c:v copy)됐으므로 지워도 됩니다.
    # 오디오 WAV 는 전사에 쓰이므로 호출자가 따로 결정합니다.
    doomed: list[Path] = []
    if delete_video and video is not None:
        doomed.append(Path(video))
    if delete_audio:
        doomed.extend(a.path for a in audio)
    for victim in doomed:
        try:
            victim.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("원본 삭제 실패(무시): %s", exc)

    return out_path
