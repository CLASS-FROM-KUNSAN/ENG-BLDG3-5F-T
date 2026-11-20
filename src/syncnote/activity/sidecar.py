"""saved-sidecar 어댑터 — C(데스크톱 Acrobat)의 **받는 쪽(Python)**.

데스크톱 Acrobat 은 AES-GCM·DPAPI 를 못 하므로, 폴더 레벨 JS(생산자)가 저장/닫기
시점에 폼 필드 값을 **사이드카 JSON** 한 개로 스테이징 폴더에 떨굽니다. 이 워처가
그 파일을 ms 단위로 읽어 정책·봉인 파이프라인에 넘기고 **즉시 삭제**합니다.
키 입력은 가로채지 않습니다(값은 이미 문서에 커밋된 것).

정직하게: 사이드카는 **평문**입니다 — 짧은 순간(워처가 읽고 지울 때까지, 보통 1초
안쪽) 디스크에 존재합니다. ACL=현재 사용자, 파일명=UUID(필드명 미노출). Python 이
안 돌던 사이에 쌓인 것은 기동 시 sweep 으로 지웁니다. 이 '평문 창'은 Acrobat JS 로는
봉인을 못 하기 때문에 생기는 본질적 한계이고, docs/ACTIVITY.md 에 명시합니다.

Acrobat 생산자(폴더 레벨 JS)는 Acrobat 이 깔린 기기에서 검증해야 하므로 **보류**입니다.
이 모듈(받는 쪽)은 합성 사이드카로 CI 에서 검증됩니다 — 사이드카 계약이 경계입니다.

사이드카 스키마(v1):
    { "v":1, "adapter":"acrobat_js",
      "doc_sha":"<PDF 내용 SHA-256>",   # 같은 문서 재저장 중복 방지(선택)
      "doc_hash":"<문서 식별자>",
      "ts": <ms>,
      "fields":[ {"name":"FullName","type":"text","flags":0,"value":"..."} , ... ] }
  또는 지원 불가:
    { "v":1, "adapter":"acrobat_js", "unsupported":"encrypted" }   # xfa | encrypted | nofields
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..logging_setup import get as get_logger
from .pipeline import ingest
from .policy import Policy

log = get_logger("activity.sidecar")

UNSUPPORTED = {"encrypted", "xfa", "nofields"}


def process_sidecar(data: dict, store, policy: Policy, state) -> list[str]:
    """사이드카 하나를 처리. 각 필드의 결과 코드 리스트를 돌려줍니다."""
    if data.get("v") != 1:
        return ["bad_version"]
    unsupported = str(data.get("unsupported", ""))
    if unsupported:
        reason = unsupported if unsupported in UNSUPPORTED else "unknown"
        log.info("Acrobat 문서 건너뜀(%s) — 저장하지 않음", reason)
        return [f"unsupported:{reason}"]

    doc_sha = str(data.get("doc_sha", ""))
    dedup_key = f"acrobat:{doc_sha}" if doc_sha else ""
    if dedup_key and state is not None and state.is_downloaded(dedup_key):
        return ["dup"]

    adapter = str(data.get("adapter", "acrobat_js"))
    doc_hash = str(data.get("doc_hash", ""))
    ts = int(data.get("ts", 0) or 0)
    outcomes: list[str] = []
    for field in data.get("fields", []) or []:
        msg = {
            "v": 1, "adapter": adapter,
            "scope": {"origin": None, "doc_hash": doc_hash, "field_path": field.get("name", "")},
            "field": field,
            "value": field.get("value", ""),
            "commit_reason": "save",
            "ts": ts,
        }
        outcomes.append(ingest(store, policy, msg))

    if dedup_key and state is not None:
        state.mark_downloaded(dedup_key)
        state.save()
    return outcomes


def process_dir(staging: Path, store, policy: Policy, state) -> dict[str, int]:
    """스테이징 폴더의 사이드카를 전부 처리하고 **각 파일을 지웁니다**.

    평문이 남지 않도록, 파싱에 실패해도 그 파일을 삭제합니다(이유 로그).
    """
    staging = Path(staging)
    tally = {"sealed": 0, "dropped": 0, "files": 0, "errors": 0}
    if not staging.exists():
        return tally
    for path in sorted(staging.glob("*.json")):
        tally["files"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for outcome in process_sidecar(data, store, policy, state):
                if outcome == "sealed":
                    tally["sealed"] += 1
                elif outcome.startswith("drop:"):
                    tally["dropped"] += 1
        except Exception as exc:
            tally["errors"] += 1
            log.warning("사이드카 처리 실패(%s): %s", path.name, exc)
        finally:
            path.unlink(missing_ok=True)      # 평문은 어떤 경우에도 남기지 않음
    return tally


def sweep(staging: Path, max_age_sec: float) -> int:
    """기동 시: Python 이 안 돌던 사이 쌓인 묵은 사이드카를 지웁니다(경고)."""
    staging = Path(staging)
    if not staging.exists():
        return 0
    now = time.time()
    removed = 0
    for path in staging.glob("*.json"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age >= max_age_sec:
            path.unlink(missing_ok=True)
            removed += 1
    if removed:
        log.warning("기동 sweep: 묵은 사이드카 %d개를 지웠습니다(평문 잔존 방지). "
                    "Python 이 꺼져 있던 동안 저장된 폼은 이번엔 봉인되지 않습니다.", removed)
    return removed


def staging_dir(cfg) -> Path:
    raw = str(cfg.get("activity.watch_acrobat.staging_dir") or "")
    if raw:
        return Path(raw)
    return Path(cfg.data_dir) / "activity" / ".staging"


def run(cfg, *, once: bool = False) -> int:
    """워처 루프. open_sealed 가 게이트를 강제하므로, 열리면 그 세션은 통과한 것."""
    from ..state import Store
    from .store import open_sealed

    store = open_sealed(cfg)
    policy = Policy.from_settings(cfg.section("activity"))
    state = Store.open(Path(cfg.data_dir) / "activity" / "acrobat_seen.json")
    staging = staging_dir(cfg)
    staging.mkdir(parents=True, exist_ok=True)
    sweep(staging, float(cfg.get("activity.watch_acrobat.sweep_minutes", 5)) * 60)
    poll = float(cfg.get("activity.watch_acrobat.poll_sec", 1.0))
    log.info("Acrobat 사이드카 워처 시작: %s (%.1f초 간격)", staging, poll)
    try:
        while True:
            tally = process_dir(staging, store, policy, state)
            if tally["files"]:
                log.info("처리 %d파일 → 봉인 %d, 드롭 %d, 오류 %d",
                         tally["files"], tally["sealed"], tally["dropped"], tally["errors"])
            if once:
                return 0
            time.sleep(poll)
    finally:
        store.close()
