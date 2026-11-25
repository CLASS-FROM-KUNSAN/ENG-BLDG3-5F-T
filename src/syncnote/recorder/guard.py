"""녹화 지킴이(record guard): 녹화 중이 아닐 때 늘, 모든 녹화를 '확인됨' 으로 모읍니다.

    python -m syncnote record guard            몇 분마다 확인(작업 스케줄러 RecordGuard)
    python -m syncnote record guard --once     한 번만
    python -m syncnote record guard --status   지금 상태

정규화: 기준은 디스크입니다. 확인할 때마다 녹화 폴더를 훑어 장부
(state/recording_ledger.json)를 다시 맞추므로, 장부가 지워져도 다음 확인에서 복원됩니다.
녹화 파일 하나는 아래 상태 중 하나이고, 앞으로만 나아갑니다.

    조각 ─합치기→ 파일 ─검사→ 검사 통과 ─업로드→ 올라감 ─릴리스 확인→ 확인됨

롤백: 단계가 틀어지면 그 단계 앞으로 되돌려 다시 합니다.
  - 검사 실패: 원본을 .work/leftover 로 옮겨 두고 다시 담기(remux) → 안 되면 audio/ 의 WAV 로
    소리만 다시 만들기. 새 파일은 임시 이름으로 만들어 검사를 통과해야 바꿔 끼웁니다.
  - 릴리스에 없거나 크기가 다름(중간에 끊긴 업로드·지워진 에셋): 업로드 기록을 지우고 다시
    올립니다(같은 이름 에셋은 업로더가 지우고 새로 올림).
  - 실패는 5분 → 15분 → 1시간 → 6시간 간격으로 다시 시도, max_attempts 번 넘으면 '격리'
    (하루 한 번 다시 시도). 원격을 못 읽으면(네트워크) 실패로 세지 않고 나중에 봅니다.
무음: 무음이어도 올립니다. 예전의 '무음 보류'(<파일>.held.json) 표시는 무시하고 올립니다.
막힘: 과목을 몰라 올릴 곳이 없거나 public 저장소 차단에 걸린 것 — 한 시간마다 다시 봅니다.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..logging_setup import get as get_logger

log = get_logger("recorder.guard")

FILE = "파일"
VERIFIED = "검사 통과"
UPLOADED = "올라감"
CONFIRMED = "확인됨"
QUARANTINE = "격리"
HELD = "보류"
BLOCKED = "막힘"
ORDER = (FILE, VERIFIED, UPLOADED, CONFIRMED, BLOCKED, HELD, QUARANTINE)

LEDGER = "recording_ledger.json"
HELD_SUFFIX = ".held.json"
MEDIA = (".mkv", ".mp4")
SKIP_DIRS = {".work", "audio", "leftover"}
BACKOFF_MIN = (5, 15, 60, 360)
REMOTE_UNKNOWN_MIN = 15
BLOCKED_RETRY_MIN = 60
QUARANTINE_RETRY_MIN = 24 * 60


class Blocked(Exception):
    """올릴 곳이 없음(과목 모름·public 차단). 실패로 세지 않습니다."""


FATAL = "fatal"            # 열리지 않음·길이 없음 — 다시 만들고, 안 되면 소리만으로
STRUCTURE = "structure"    # 담는 틀(컨테이너)이 일부 망가짐 — 다시 담기(remux), 안 되면 원본 그대로
FRAMES = "frames"          # 화면 몇 장만 깨짐 — 손대지 않고 그대로 올림(고칠 데이터가 없음)


@dataclass
class Problem:
    msg: str
    level: str = FATAL

    def __str__(self) -> str:
        return self.msg


def level_of(problem) -> str:
    """검사 결과의 심각도. 글자만 돌려준 검사(옛 훅·테스트)는 치명으로 봅니다."""
    return getattr(problem, "level", FATAL)


# ---------------------------------------------------------------------------
# 보류 표시(예전 것 — 지금은 업로드를 막지 않음. 옛 표시를 알아보는 데만 씀)
# ---------------------------------------------------------------------------
def held_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + HELD_SUFFIX)


def mark_held(path: Path, reason: str) -> None:
    try:
        held_path(path).write_text(json.dumps({"reason": reason, "at": time.time()},
                                              ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("보류 표시를 남기지 못했습니다(%s): %s", Path(path).name, exc)


def is_held(path: Path) -> bool:
    return held_path(path).exists()


def media_files(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*"):
        if p.suffix.lower() not in MEDIA or not p.is_file():
            continue
        rel = p.relative_to(root).parts
        if any(part in SKIP_DIRS or part.startswith(".parts-") for part in rel[:-1]):
            continue
        if ".rebuild." in p.name:
            continue                                  # 다시 만드는 중인 임시 파일
        out.append(p)
    return sorted(out)


# ---------------------------------------------------------------------------
# 장부
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    path: str
    state: str = FILE
    stage: str = FILE              # 격리 전에 머물던 단계(풀리면 여기서 다시)
    attempts: int = 0
    next_try: float = 0.0
    last_error: str = ""
    size: int = -1
    mtime: float = -1.0
    digest: str = ""
    tag: str = ""
    asset: str = ""
    history: list[str] = field(default_factory=list)

    def note(self, msg: str, now: float) -> None:
        self.history = (self.history + [f"{time.strftime('%m-%d %H:%M', time.localtime(now))} {msg}"])[-6:]


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.entries: dict[str, Entry] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for k, v in (data.get("entries") or {}).items() if isinstance(data, dict) else []:
            try:
                self.entries[k] = Entry(**v)
            except TypeError:
                continue

    def save(self) -> None:
        from ..state import _atomic_write

        _atomic_write(self.path, json.dumps(
            {"version": 1, "entries": {k: asdict(e) for k, e in self.entries.items()}},
            ensure_ascii=False, indent=1))

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(ORDER, 0)
        for e in self.entries.values():
            out[e.state] = out.get(e.state, 0) + 1
        return out


# ---------------------------------------------------------------------------
# 지킴이
# ---------------------------------------------------------------------------
@dataclass
class Hooks:
    busy: Callable[[], str | None]                     # 쉬어야 하는 이유(없으면 None)
    recover: Callable[[], object]                      # 조각 → 파일 (#83 복구)
    verify: Callable[[Path], object]                   # Problem/글자(없으면 None)
    rebuild: Callable[[Path, bool], str]               # (파일, 소리만도 허용) → 방법. 못 하면 예외
    digest: Callable[[Path], str]
    record: Callable[[str], dict | None]               # 업로드 기록(없으면 None)
    upload: Callable[[Path], dict]                     # 올리고 기록을 돌려줌. Blocked / 예외
    remote: Callable[[Path, dict], bool | None]        # 릴리스에 정말 있나(모르면 None)
    forget: Callable[[str], None]                      # 업로드 기록 지우기(롤백)


@dataclass
class Summary:
    skipped: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)


class Guard:
    def __init__(self, root: Path, ledger: Ledger, hooks: Hooks, *, max_attempts: int = 6,
                 verify_remote: bool = True, now: Callable[[], float] = time.time) -> None:
        self.root, self.ledger, self.h = Path(root), ledger, hooks
        self.max_attempts = max(1, int(max_attempts))
        self.verify_remote = verify_remote
        self.now = now

    # -- 한 번 확인 -----------------------------------------------------
    def tick(self) -> Summary:
        reason = self.h.busy()
        if reason:
            return Summary(skipped=reason, counts=self.ledger.counts())
        try:
            self.h.recover()
        except Exception:
            log.exception("녹화 조각 복구 중 오류(나머지 확인은 계속)")
        changes: list[str] = []
        seen = set()
        for f in media_files(self.root):
            key = str(f)
            seen.add(key)
            e = self.ledger.entries.get(key) or Entry(key)
            self.ledger.entries[key] = e
            before = e.state
            try:
                self._step(f, e)
            except Exception as exc:                       # 지킴이는 멈추지 않음
                log.exception("녹화 지킴이: %s 처리 중 오류", f.name)
                self._fail(e, f"예기치 못한 오류: {exc}")
            if e.state != before:
                changes.append(f"{f.name}: {before} → {e.state}")
        for gone in [k for k in self.ledger.entries if k not in seen]:
            del self.ledger.entries[gone]                 # 지워졌거나(올린 뒤 삭제 설정) 옮겨진 파일
        self.ledger.save()
        for c in changes:
            log.info("녹화 지킴이: %s", c)
        return Summary(counts=self.ledger.counts(), changes=changes)

    # -- 한 파일 --------------------------------------------------------
    def _step(self, f: Path, e: Entry) -> None:
        now = self.now()
        st = f.stat()
        if (e.size, e.mtime) != (st.st_size, st.st_mtime):
            if e.size != -1:
                e.note("파일이 바뀜 — 처음부터 다시 확인", now)
            e.size, e.mtime, e.digest = st.st_size, st.st_mtime, ""
            e.state = e.stage = FILE
            e.attempts, e.next_try, e.last_error = 0, 0.0, ""
        # 예전 '무음이라 보류(.held.json)' 는 더 이상 업로드를 막지 않습니다(무음 감지 시 업로드
        # 차단 금지). 보류였던 것도 처음부터 확인해 올립니다.
        if e.state == HELD:
            e.state = e.stage = FILE
            e.note("무음 보류 해제 — 무음이어도 올림", now)
        if e.state == CONFIRMED or e.next_try > now:
            return
        if e.state == QUARANTINE:
            e.state = e.stage                             # 하루 지났음 — 머물던 단계부터 다시
        if e.state == BLOCKED:
            e.state = VERIFIED

        if e.state == FILE:
            if not self._check(f, e, now):
                return
            self._advance(e, VERIFIED)

        if e.state == VERIFIED:
            if not e.digest:
                e.digest = self.h.digest(f)
            rec = self.h.record(e.digest)
            if rec is None:
                try:
                    rec = self.h.upload(f)
                except Blocked as exc:
                    e.state, e.last_error = BLOCKED, str(exc)
                    e.next_try = now + BLOCKED_RETRY_MIN * 60
                    return
                except Exception as exc:
                    self._fail(e, f"업로드 실패: {exc}")
                    return
                e.note(f"올림 → {rec.get('tag', '')}", now)
            e.tag, e.asset = str(rec.get("tag", "")), str(rec.get("asset", ""))
            self._advance(e, UPLOADED)

        if e.state == UPLOADED:
            if not self.verify_remote:
                self._advance(e, CONFIRMED)
                return
            rec = self.h.record(e.digest) or {"tag": e.tag, "asset": e.asset, "size": e.size}
            try:
                ok = self.h.remote(f, rec)
            except Exception as exc:
                log.info("녹화 지킴이: 릴리스를 확인하지 못했습니다(나중에 다시): %s", exc)
                ok = None
            if ok is None:
                e.next_try = now + REMOTE_UNKNOWN_MIN * 60
                return
            if ok:
                self._advance(e, CONFIRMED)
                return
            # 롤백: 기록을 지우고 '검사 통과' 로 되돌려 다시 올림
            self.h.forget(e.digest)
            e.state = e.stage = VERIFIED
            e.note(f"릴리스({e.tag})에 {e.asset} 가 없거나 크기가 다름 → 다시 올림", now)
            log.warning("녹화 지킴이: %s 가 릴리스에 없거나 크기가 다릅니다 — 다시 올립니다", f.name)
            self._fail(e, "릴리스에 없거나 크기가 다름")

    def _check(self, f: Path, e: Entry, now: float) -> bool:
        """검사하고 필요하면 다시 만듭니다. 올려도 되면 True.

        내용을 잃지 않는 쪽을 고릅니다: 화면 대부분이 살아 있는 파일을 소리만 든 파일로
        바꾸지 않습니다. 소리만으로 다시 만드는 것은 파일이 아예 열리지 않을 때뿐입니다.
        """
        problem = self.h.verify(f)
        if not problem:
            return True
        level = level_of(problem)
        if level == FRAMES:
            e.note(f"{problem} — 고칠 데이터가 없어 그대로 올림", now)
            return True
        try:
            how = self.h.rebuild(f, level == FATAL)
        except Exception as exc:
            if level == FATAL:
                self._fail(e, f"{problem} / 다시 만들기 실패: {exc}")
                return False
            e.note(f"{problem} — 다시 담지 못해({exc}) 원본 그대로 올림", now)
            return True
        st = f.stat()
        e.size, e.mtime, e.digest = st.st_size, st.st_mtime, ""
        again = self.h.verify(f)
        if again and level_of(again) == FATAL:
            self._fail(e, f"다시 만든 뒤에도 열리지 않음: {again}")
            return False
        e.note(f"검사({problem}) → {how}" + (f" · 남은 문제: {again}" if again else ""), now)
        log.warning("녹화 지킴이: %s 검사(%s) → %s", f.name, problem, how)
        return True

    def _advance(self, e: Entry, state: str) -> None:
        e.state = e.stage = state
        e.attempts, e.next_try, e.last_error = 0, 0.0, ""

    def _fail(self, e: Entry, msg: str) -> None:
        now = self.now()
        e.attempts += 1
        e.last_error = msg
        e.note(f"실패 {e.attempts}회: {msg}", now)
        if e.state in (FILE, VERIFIED, UPLOADED):
            e.stage = e.state
        if e.attempts >= self.max_attempts:
            e.state = QUARANTINE
            e.next_try = now + QUARANTINE_RETRY_MIN * 60
            log.error("녹화 지킴이: %s 를 %d번 처리하지 못해 격리합니다(하루 뒤 다시): %s",
                      Path(e.path).name, e.attempts, msg)
        else:
            e.next_try = now + BACKOFF_MIN[min(e.attempts - 1, len(BACKOFF_MIN) - 1)] * 60


# ---------------------------------------------------------------------------
# 실제 검사 · 다시 만들기
# ---------------------------------------------------------------------------
def _last_line(text: str) -> str:
    lines = [x.strip() for x in str(text).splitlines() if x.strip()]
    return lines[-1][:200] if lines else ""


_CONTAINER_HINTS = ("[in#", "matroska", "mov,mp4", "EBML", "Invalid data", "moov atom",
                    "Truncating packet", "invalid as first byte")


def verify_media(path: Path) -> Problem | None:
    """파일이 쓸 만한가. 길이를 읽고 **처음부터 끝까지** 실제로 풀어 봅니다.

    끝부분만 보면 가운데가 망가진 파일을 놓칩니다(실측: 가운데 40% 가 0 인 mkv 가 끝 15초
    검사를 통과). ffmpeg 는 망가진 곳이 있어도 종료 코드 0 이라 오류 줄로 판단합니다.
    30초짜리 풀기에 약 1초 — 30분 세그먼트면 1분 남짓이고, 파일마다 한 번뿐입니다.
    ffmpeg 가 없으면 검사를 건너뜁니다.
    """
    from .ffmpeg_util import FFmpegMissing, duration_of, find_ffmpeg

    try:
        ff = find_ffmpeg()
    except FFmpegMissing:
        return None
    dur = duration_of(path)
    if dur is None or dur <= 0:
        return Problem("길이를 읽을 수 없습니다(망가졌거나 비어 있는 파일)", FATAL)
    try:
        r = subprocess.run([ff, "-v", "error", "-i", str(path), "-f", "null", "-"],
                           capture_output=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Problem(f"끝까지 풀어 보지 못했습니다({exc})", FATAL)
    lines = [x.strip() for x in r.stderr.decode("utf-8", "replace").splitlines() if x.strip()]
    if r.returncode != 0:
        return Problem("풀 수 없습니다: " + (lines[-1][:200] if lines else f"코드 {r.returncode}"),
                       FATAL)
    if not lines:
        return None
    if any(h in x for x in lines for h in _CONTAINER_HINTS):
        return Problem(f"파일 구조가 일부 망가졌습니다({len(lines)}줄): {lines[0][:160]}", STRUCTURE)
    return Problem(f"화면 일부가 깨져 있습니다({len(lines)}곳): {lines[0][:160]}", FRAMES)


def rebuild_media(path: Path, sound_only: bool = True, *,
                  verify: Callable[[Path], object] = verify_media,
                  mux: Callable | None = None) -> str:
    """망가진 녹화 파일을 다시 만듭니다. 원본은 .work/leftover 로 옮겨 둡니다(되돌릴 수 있게).

    1) 다시 담기(remux, 화질 손실 없음) — 결과가 깨끗하거나 화면 몇 장만 깨졌으면 채택.
    2) sound_only 일 때만: audio/ 의 WAV 로 소리만 든 파일. 원본이 아예 안 열릴 때만 씁니다.
    """
    from .ffmpeg_util import find_ffmpeg
    from .mux import MuxInput
    from .mux import mux as real_mux

    path = Path(path)
    tmp = path.with_name(f"{path.stem}.rebuild{path.suffix}")
    tmp.unlink(missing_ok=True)

    def swap(how: str) -> str:
        keep = path.parent / ".work" / "leftover"
        keep.mkdir(parents=True, exist_ok=True)
        path.replace(keep / f"{path.name}.bad-{time.strftime('%Y%m%d%H%M%S')}")
        tmp.replace(path)
        return how

    ff = find_ffmpeg()
    r = subprocess.run([ff, "-y", "-v", "error", "-err_detect", "ignore_err", "-i", str(path),
                        "-map", "0", "-c", "copy", str(tmp)], capture_output=True, timeout=1800)
    if r.returncode == 0 and tmp.exists():
        left = verify(tmp)
        if not left or level_of(left) == FRAMES:
            return swap("다시 담기(remux, 화질 손실 없음)")
    tmp.unlink(missing_ok=True)
    if not sound_only:
        raise RuntimeError("다시 담아도 구조가 고쳐지지 않았습니다")

    stem = path.stem.split("_mon")[0]
    wavs = sorted((path.parent / "audio").glob(f"{stem}.*.wav"))
    inputs = [MuxInput(w, w.stem.rsplit(".", 1)[-1], 0.0) for w in wavs if w.stat().st_size > 4096]
    if inputs:
        (mux or real_mux)(tmp, None, 0.0, inputs, delete_video=False, delete_audio=False)
        left = verify(tmp) if tmp.exists() else Problem("만들어지지 않음")
        if tmp.exists() and (not left or level_of(left) != FATAL):
            return swap("화면을 살릴 수 없어 소리만으로 다시 만듦")
        tmp.unlink(missing_ok=True)
    raise RuntimeError("다시 만들 재료가 없습니다(다시 담기 실패, 소리 파일 없음)")


def status_text(ledger: Ledger, *, limit: int = 30) -> str:
    c = ledger.counts()
    lines = ["녹화 지킴이 상태: " + " · ".join(f"{s} {c.get(s, 0)}" for s in ORDER)]
    todo = [e for e in ledger.entries.values() if e.state != CONFIRMED]
    for e in sorted(todo, key=lambda e: (ORDER.index(e.state) if e.state in ORDER else 99, e.path))[:limit]:
        nxt = (time.strftime("%m-%d %H:%M", time.localtime(e.next_try))
               if e.next_try > time.time() else "다음 확인 때")
        lines.append(f"  [{e.state}] {Path(e.path).name}  시도 {e.attempts}  다음 {nxt}"
                     + (f"\n      마지막 오류: {e.last_error}" if e.last_error else ""))
        lines.extend(f"      · {h}" for h in e.history[-3:])
    if len(todo) > limit:
        lines.append(f"  … 외 {len(todo) - limit}개")
    return "\n".join(lines)
