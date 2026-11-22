"""ffmpeg 설치 시도 이력과 '같은 실패 반복' 막기.

    tools\\.ffmpeg_install.json
      "winget": {...}          다시 해도 같은 winget 실패(ffmpeg_fetch.auto 가 씀)
      "runs":   [...최근 20회]  실행마다: 시각, 출처별 결과, 시도한 버전·이유·종류·해시, 결과

건너뛰기는 따로 저장하지 않고 **이력에서 계산합니다**(이력을 지우면 풀림, --retry-skipped
로도 풀림).

  API 한도 초과      마지막 기록의 '풀리는 시각'까지 API 를 안 부름
  같은 출처 3번 연속 막힘·연결 실패·gh 확인 실패 -> 마지막 실패 뒤 24시간 동안 건너뜀
                     ('gh 없음'·'로그인 안 됨'·'git 없음' 은 확인이 싸서 세지 않음.
                      건너뛴 실행은 세지 않음 — 24시간 뒤 다시 실패하면 또 24시간)
  녹화에 필요한 기능이 없던 빌드  그 해시는 다시 받지 않음(같은 파일이면 결과가 같음)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

KEEP = 20
STREAK = 3
COOLDOWN = timedelta(hours=24)
SKIPPED = "건너뜀"
# 세지 않는(확인이 싼) 이유
_CHEAP = ("gh 없음", "git 없음", "gh 로그인 안 됨")
_RESET = re.compile(r"한도 초과 \((\d{2}):(\d{2})에 풀림\)")


def load(path: Path | None) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def update(path: Path | None, key: str, value) -> None:
    """파일을 다시 읽어 그 키만 바꿉니다(다른 쪽이 쓴 기록을 덮지 않게)."""
    if not path:
        return
    data = load(path)
    data[key] = value
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass                                # 기록 실패로 설치를 멈추지 않음


def record(path: Path | None, run: dict) -> None:
    runs = list(load(path).get("runs") or [])
    runs.append(run)
    update(path, "runs", runs[-KEEP:])


def make_run(*, at: datetime, reports, tried: list[tuple[str, str, str, str]], ok: bool,
             used: str = "", skipped: dict[str, str] | None = None) -> dict:
    return {
        "at": at.isoformat(timespec="seconds"),
        "sources": {r.name: {"ok": bool(r.ok), "note": r.note} for r in reports
                    if r.name in ("API", "gh", "git")},
        "skipped": dict(skipped or {}),
        "tried": [{"version": v, "reason": why, "kind": k, "sha256": s} for v, why, k, s in tried],
        "ok": ok,
        "used": used,
    }


def _reset_at(note: str, at: datetime) -> datetime | None:
    m = _RESET.search(note or "")
    if not m:
        return None
    t = at.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    return t if t > at else t + timedelta(days=1)


def skips(data: dict, now: datetime | None = None) -> dict[str, str]:
    """지금 건너뛸 출처 -> 이유."""
    now = now or datetime.now()
    runs = data.get("runs") or []
    out: dict[str, str] = {}
    for name in ("API", "gh", "git"):
        seen = []                           # (시각, 결과) — 건너뛴 실행은 뺌
        for r in runs:
            s = (r.get("sources") or {}).get(name)
            if not s or str(s.get("note", "")).startswith(SKIPPED):
                continue
            try:
                seen.append((datetime.fromisoformat(r["at"]), s))
            except (KeyError, ValueError):
                continue
        if not seen:
            continue
        at, last = seen[-1]
        if name == "API":
            until = _reset_at(str(last.get("note", "")), at)
            if until and now < until:
                out[name] = f"한도 초과 — {until:%H:%M} 까지 안 부름"
                continue
        tail = seen[-STREAK:]
        bad = [s for _a, s in tail if not s.get("ok")
               and not any(c in str(s.get("note", "")) for c in _CHEAP)
               and not _RESET.search(str(s.get("note", "")))]          # 한도는 위 규칙으로
        if len(tail) == STREAK and len(bad) == STREAK and now - at < COOLDOWN:
            until = at + COOLDOWN
            out[name] = (f"{STREAK}번 연속 실패({str(last.get('note', ''))[:40]}) — "
                         f"{until:%m-%d %H:%M} 까지 건너뜀")
    return out


def bad_builds(data: dict) -> dict[str, str]:
    """녹화에 필요한 기능이 없던 빌드의 해시 -> 이유."""
    out: dict[str, str] = {}
    for r in data.get("runs") or []:
        for t in r.get("tried") or []:
            if t.get("kind") == "feature" and t.get("sha256"):
                out[t["sha256"]] = f"{t.get('version')}: {t.get('reason')}"
    return out


def describe(data: dict, limit: int = 10) -> list[str]:
    """--history 출력."""
    lines = []
    for r in (data.get("runs") or [])[-limit:]:
        src = " · ".join(f"{n} {'됨' if s.get('ok') else '안 됨'}"
                         for n, s in (r.get("sources") or {}).items())
        res = f"성공 {r.get('used')}" if r.get("ok") else "실패"
        lines.append(f"{r.get('at')}  {res}  [{src}]")
        for t in r.get("tried") or []:
            lines.append(f"    - {t.get('version')}: {t.get('reason')}")
        for n, why in (r.get("skipped") or {}).items():
            lines.append(f"    · {n} 건너뜀: {why}")
    return lines
