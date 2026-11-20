"""설치·점검 시도 이력 — 같은 실패를 저장할 때마다 똑같이 되풀이하지 않게.

`<data_dir>/state/setup_history.json` 에 최근 실행 몇 번의 단계별 결과를 남깁니다.

  {"runs": [{"at": "...", "steps": {"install": {"state": "fail", "cause": "network",
                                                 "remedies": ["chromium"]}, ...}}, ...]}

- repeated(): 그 단계가 최근 실행들에서 **연달아** 같은 원인으로 실패한 횟수.
  설치·점검 창은 2 이상이면 그 단계의 자동 재시도를 생략하고 수동 방법을 보여 줍니다.
- remedy_failed(): 지난 실행에서 그 해결책을 썼는데도 설치가 끝나지 않았나.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

KEEP = 20
BAD = ("fail", "warn")


def load(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def append(path: Path, steps: dict[str, dict], keep: int = KEEP) -> list[dict]:
    """이번 실행을 맨 앞에 넣고 저장. 실패해도 설치·점검은 계속(이력은 부가 기능)."""
    runs = load(path)
    runs.insert(0, {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "steps": steps})
    runs = runs[:keep]
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=1) + "\n",
                     encoding="utf-8")
    except OSError:
        pass
    return runs


def repeated(runs: list[dict], step: str, cause: str) -> int:
    """최근 실행부터 거슬러, 그 단계가 같은 원인으로 연달아 실패한 횟수."""
    n = 0
    for r in runs:
        s = (r.get("steps") or {}).get(step) or {}
        if s.get("state") in BAD and s.get("cause") == cause:
            n += 1
        else:
            break
    return n


def remedy_failed(runs: list[dict], step: str, remedy: str) -> bool:
    """바로 지난 실행에서 그 해결책을 썼는데 그 단계가 끝내 성공하지 못했나."""
    if not runs:
        return False
    s = (runs[0].get("steps") or {}).get(step) or {}
    return remedy in (s.get("remedies") or []) and s.get("state") in BAD
