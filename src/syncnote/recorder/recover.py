"""녹화 도중 컴퓨터가 꺼졌을 때 남은 조각을 다음 시작 때 파일로 만들기.

녹화 중에는 화면을 `<세션>/.work/<stem>[_monN].video.<mkv|mp4>` 에, 소리를
`<세션>/.work/<stem>.<트랙>.wav` 에 따로 쓰고, 세그먼트가 정상으로 끝날 때만
합쳐서(mux) 올립니다. 전원이 꺼지면 합치기도 업로드도 일어나지 않아 그 세그먼트가
릴리스에 영영 올라가지 않았습니다.

세그먼트를 시작할 때 `.work/<stem>.journal.json` 에 과목·라벨·트랙별 시작 시각·메타를
적어 두고, 정상으로 끝나면 지웁니다. 다음에 녹화 프로그램이 시작할 때 `.work` 에
남은 조각을 찾아
  1. WAV 머리(길이 칸)를 실제 파일 크기에 맞게 고치고(전원이 꺼지면 어긋남),
  2. 평소와 같은 mux 로 합치고(화면 파일이 망가졌으면 소리만으로),
  3. 평소 세그먼트와 같은 후처리(전사 대기열 · 올릴 목록 · 업로드)로 넘깁니다.
일지가 없는 조각(이 기능 전의 녹화)도 같은 stem 끼리 묶어 시작 시각 없이 합칩니다.
"""

from __future__ import annotations

import json
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.recover")

JOURNAL = ".journal.json"
LEFTOVER_DIR = "leftover"          # 처리하고도 남은 조각(망가진 화면 등)은 여기로 — 매번 다시 안 봄
_STEM = re.compile(r"^(\d{8}_\d{6}_part\d{3})")
_VIDEO = re.compile(r"^(?P<stem>\d{8}_\d{6}_part\d{3})(?P<sfx>_mon\d+)?\.video\.(?P<ext>mkv|mp4)$")
_WAV = re.compile(r"^(?P<stem>\d{8}_\d{6}_part\d{3})\.(?P<name>[A-Za-z0-9_-]+)\.wav$")
RECENT_SEC = 120                   # 이만큼 안에 바뀐 조각은 지금 녹화 중일 수 있어 건드리지 않음


def journal_path(work: Path, stem: str) -> Path:
    return Path(work) / f"{stem}{JOURNAL}"


def write_journal(work: Path, stem: str, data: dict) -> None:
    """세그먼트 일지. 녹화 중 여러 번 덮어씁니다(시작 시각이 정해질 때마다)."""
    from ..state import _atomic_write

    try:
        _atomic_write(journal_path(work, stem), json.dumps(data, ensure_ascii=False, indent=1))
    except OSError as exc:              # 일지를 못 써도 녹화는 계속
        log.warning("녹화 일지를 쓰지 못했습니다(꺼지면 복구가 덜 정확해짐): %s", exc)


def clear_journal(work: Path, stem: str) -> None:
    try:
        journal_path(work, stem).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# WAV 머리 고치기
# ---------------------------------------------------------------------------
def repair_wav(path: Path) -> bool:
    """RIFF 크기와 data 크기를 실제 파일 크기에 맞춥니다. 고쳤으면 True.

    python wave 는 쓰는 동안 머리를 고쳐 쓰지만, 전원이 꺼지면 머리와 실제로 디스크에
    남은 데이터 양이 어긋납니다(0 이거나 더 큼). 그러면 ffmpeg 가 소리를 덜 읽거나 못 읽습니다.
    """
    path = Path(path)
    size = path.stat().st_size
    if size < 12:
        return False
    with path.open("r+b") as fh:
        head = fh.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return False
        pos, block = 12, 1
        while pos + 8 <= size:
            fh.seek(pos)
            cid, clen = struct.unpack("<4sI", fh.read(8))
            if cid == b"fmt ":
                fmt = fh.read(min(clen, 16))
                if len(fmt) >= 14:
                    block = max(1, struct.unpack("<H", fmt[12:14])[0])
            if cid == b"data":
                real = size - (pos + 8)
                real -= real % block                 # 반쯤 쓰인 마지막 표본은 버림
                changed = False
                if clen != real:
                    fh.seek(pos + 4)
                    fh.write(struct.pack("<I", real))
                    changed = True
                riff = pos + 8 + real - 8
                if struct.unpack("<I", head[4:8])[0] != riff:
                    fh.seek(4)
                    fh.write(struct.pack("<I", riff))
                    changed = True
                return changed
            pos += 8 + clen + (clen & 1)
    return False


def _has_audio(path: Path) -> bool:
    try:
        return Path(path).stat().st_size > 44 + 4096       # 머리 + 아주 짧은 것은 빈 녹음
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 남은 조각 찾기
# ---------------------------------------------------------------------------
@dataclass
class Leftover:
    work: Path
    stem: str
    videos: list[tuple[Path, str]] = field(default_factory=list)     # (파일, _monN)
    wavs: dict[str, Path] = field(default_factory=dict)
    journal: dict = field(default_factory=dict)

    @property
    def out_dir(self) -> Path:
        return self.work.parent

    @property
    def started(self) -> datetime:
        s = self.journal.get("started")
        if s:
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                pass
        return datetime.strptime(self.stem[:15], "%Y%m%d_%H%M%S")

    def files(self) -> list[Path]:
        out = [p for p, _ in self.videos] + list(self.wavs.values())
        j = journal_path(self.work, self.stem)
        return out + ([j] if j.exists() else [])

    def newest_mtime(self) -> float:
        times = []
        for p in self.files():
            try:
                times.append(p.stat().st_mtime)
            except OSError:
                pass
        return max(times, default=0.0)


def find_leftovers(recordings_root: Path) -> list[Leftover]:
    root = Path(recordings_root)
    if not root.is_dir():
        return []
    found: dict[tuple[Path, str], Leftover] = {}
    for work in sorted(root.rglob(".work")):
        if not work.is_dir():
            continue
        for p in sorted(work.iterdir()):
            if not p.is_file():
                continue
            m = _VIDEO.match(p.name)
            w = _WAV.match(p.name)
            stem = (m or w).group("stem") if (m or w) else None
            if stem is None and p.name.endswith(JOURNAL):
                s = _STEM.match(p.name)
                stem = s.group(1) if s else None
            if stem is None:
                continue
            lo = found.setdefault((work, stem), Leftover(work, stem))
            if m:
                lo.videos.append((p, m.group("sfx") or ""))
            elif w:
                lo.wavs[w.group("name")] = p
            else:
                try:
                    lo.journal = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    lo.journal = {}
    return list(found.values())


# ---------------------------------------------------------------------------
# 합치기
# ---------------------------------------------------------------------------
@dataclass
class Recovered:
    leftover: Leftover
    finals: list[Path] = field(default_factory=list)
    pending: Path | None = None          # ffmpeg 가 없어 `record remux` 로 미룸
    audio_only: bool = False             # 화면 파일이 망가져 소리만 살림
    notes: list[str] = field(default_factory=list)

    def segment(self):
        from .ffmpeg_util import duration_of
        from .session import Segment

        lo = self.leftover
        path = self.finals[0] if self.finals else lo.out_dir / f"{lo.stem}.{_container(lo)}"
        seconds = (duration_of(path) or 0.0) if self.finals else 0.0
        return Segment(int(lo.journal.get("index") or int(lo.stem[-3:])), path, lo.started,
                       seconds, list(lo.wavs), dict(lo.wavs))


def _container(lo: Leftover, default: str = "mkv") -> str:
    if lo.journal.get("container"):
        return str(lo.journal["container"])
    for p, _ in lo.videos:
        return p.suffix.lstrip(".")
    return default


def _last_line(exc: Exception) -> str:
    """ffmpeg 오류는 여러 줄 — 원인이 적힌 마지막 줄만."""
    lines = [x.strip() for x in str(exc).splitlines() if x.strip()]
    return lines[-1][:200] if lines else type(exc).__name__


def _park(lo: Leftover, paths: list[Path]) -> None:
    """처리하고도 남은 조각을 .work/leftover 로 — 다음 시작 때 또 붙잡지 않게."""
    dest = lo.work / LEFTOVER_DIR
    for p in paths:
        if p.exists():
            dest.mkdir(exist_ok=True)
            try:
                p.replace(dest / p.name)
            except OSError as exc:
                log.warning("남은 조각을 옮기지 못했습니다(%s): %s", p.name, exc)


def finalize(lo: Leftover, cfg, *, mux=None, ffmpeg_ok: bool | None = None) -> Recovered:
    """남은 조각 하나(stem) -> 최종 파일. 평소 세그먼트 마무리와 같은 순서."""
    from .meta import RecordingMeta
    from .mux import MuxInput
    from .mux import mux as real_mux

    mux = mux or real_mux
    j = lo.journal
    rec = Recovered(lo)
    container = _container(lo, str(cfg.get("recording.container", "mkv")))
    layout = str(j.get("layout") or cfg.get("recording.audio.track_layout", "separate"))
    meta = None
    if j.get("meta"):
        try:
            meta = RecordingMeta.from_state(j["meta"])
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("녹화 일지의 수업 정보를 읽지 못해 태그 없이 만듭니다: %s", exc)
    starts = {k: float(v or 0) for k, v in (j.get("audio") or {}).items()}
    vstarts = {k: float(v or 0) for k, v in (j.get("video") or {}).items()}
    index = int(j.get("index") or int(lo.stem[-3:]))

    for p in lo.wavs.values():
        try:
            if repair_wav(p):
                rec.notes.append(f"{p.name}: 머리 길이를 실제 크기에 맞춤")
        except OSError as exc:
            rec.notes.append(f"{p.name}: 머리를 고치지 못함({exc})")
    inputs = [MuxInput(p, name, starts.get(name, 0.0)) for name, p in lo.wavs.items()
              if _has_audio(p)]

    if ffmpeg_ok is None:
        from .ffmpeg_util import FFmpegMissing, find_ffmpeg
        try:
            find_ffmpeg()
            ffmpeg_ok = True
        except FFmpegMissing:
            ffmpeg_ok = False
    if not ffmpeg_ok:
        if inputs:
            from .remux import write_pending
            rec.pending = write_pending(
                lo.out_dir / f"{lo.stem}.{container}",
                [(i.name, i.path, i.started_at) for i in inputs], layout=layout, meta=meta,
                segment_index=index, delete_wav=False, course_id=str(j.get("course_id") or ""),
                started=lo.started)
        rec.notes.append("ffmpeg 가 없어 파일은 `record remux` 로 미룸(화면 조각은 남겨 둠)")
        clear_journal(lo.work, lo.stem)
        return rec

    targets = lo.videos or [(None, "")]
    for video, sfx in targets:
        final = lo.out_dir / f"{lo.stem}{sfx}.{container}"
        try:
            mux(final, video, vstarts.get(sfx, 0.0), inputs, layout=layout,
                delete_video=True, delete_audio=False, meta=meta, segment_index=index)
            rec.finals.append(final)
            continue
        except Exception as exc:
            what = f"(모니터 {sfx[4:]})" if sfx else ""
            if video is None or not inputs:
                rec.notes.append(f"합치기 실패{what}: {_last_line(exc)}")
                continue
            rec.notes.append(f"화면 파일{what}이 망가져 소리만으로 만들었습니다: {_last_line(exc)}")
        try:                                       # 화면이 망가졌으면 소리라도 살림
            mux(final, None, 0.0, inputs, layout=layout, delete_video=False,
                delete_audio=False, meta=meta, segment_index=index)
            rec.finals.append(final)
            rec.audio_only = True
        except Exception as exc:
            rec.notes.append(f"소리만으로도 합치기 실패: {_last_line(exc)}")

    if rec.finals:
        from .remux import pending_path
        for f in rec.finals:                       # 예전에 ffmpeg 가 없어 미뤄 둔 것 — 이제 필요 없음
            pending_path(f).unlink(missing_ok=True)
        keep_wav = bool(cfg.get("transcribe.enabled", True))
        moved: dict[str, Path] = {}
        audio_dir = lo.out_dir / "audio"
        for name, p in lo.wavs.items():
            if not p.exists():
                continue
            if keep_wav:
                audio_dir.mkdir(exist_ok=True)
                try:
                    p.replace(audio_dir / p.name)
                    moved[name] = audio_dir / p.name
                except OSError:
                    moved[name] = p
            else:
                p.unlink(missing_ok=True)
        lo.wavs = moved
        clear_journal(lo.work, lo.stem)
        _park(lo, [p for p, _ in lo.videos])        # 망가져서 못 쓴 화면 조각
        for log_file in lo.work.glob(f"{lo.stem}*.ffmpeg.log"):
            _park(lo, [log_file])
    try:
        if not any(lo.work.iterdir()):
            lo.work.rmdir()
    except OSError:
        pass
    return rec


def recover_all(recordings_root: Path, cfg, *, busy: bool = False, mux=None,
                ffmpeg_ok: bool | None = None, now: float | None = None) -> list[Recovered]:
    """남은 조각을 모두 처리. busy(지금 녹화 중)면 방금 바뀐 조각은 건드리지 않습니다."""
    now = time.time() if now is None else now
    out = []
    for lo in find_leftovers(recordings_root):
        if not lo.videos and not lo.wavs:
            clear_journal(lo.work, lo.stem)            # 일지만 남음 — 시작하자마자 꺼짐
            continue
        if busy and now - lo.newest_mtime() < RECENT_SEC:
            log.info("지금 녹화 중인 조각으로 보여 건너뜁니다: %s", lo.stem)
            continue
        log.warning("끝나지 않은 녹화 조각을 찾았습니다(컴퓨터가 녹화 중에 꺼진 것 같습니다): "
                    "%s / %s", lo.out_dir, lo.stem)
        try:
            rec = finalize(lo, cfg, mux=mux, ffmpeg_ok=ffmpeg_ok)
        except Exception:
            log.exception("녹화 조각 복구 실패: %s", lo.stem)
            continue
        for n in rec.notes:
            log.warning("  %s", n)
        if rec.finals:
            log.info("  복구한 파일: %s", ", ".join(p.name for p in rec.finals))
        out.append(rec)
    return out
