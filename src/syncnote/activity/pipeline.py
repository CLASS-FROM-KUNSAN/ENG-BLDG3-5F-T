"""ingest: 들어온 필드 커밋 → 정책 판정 → (담기면) 봉인 저장.

게이트는 저장소를 열 때(`store.open_sealed`) 한 번 강제하므로 여기서 매 이벤트마다
다시 돌리지 않습니다(필드 커밋마다 PowerShell 프로브를 돌리면 느립니다). 저장소가
존재한다 = 그 세션 시작 시점에 게이트를 통과했다는 뜻입니다.
"""

from __future__ import annotations

from typing import Any

from .policy import FieldEvent, Policy


def event_from_message(msg: dict[str, Any]) -> FieldEvent:
    scope = msg.get("scope", {}) or {}
    field = msg.get("field", {}) or {}
    ftype = str(field.get("type", "text"))
    return FieldEvent(
        adapter=str(msg.get("adapter", "")),
        value=str(msg.get("value", "")),
        field_name=str(field.get("name", scope.get("field_path", ""))),
        field_type="Sig" if ftype == "signature" else ftype,
        is_password=bool(field.get("is_password", False)),
        is_signature=ftype == "signature" or bool(field.get("is_signature", False)),
        is_richtext=bool(field.get("is_richtext", False)),
        flags=int(field.get("flags", 0) or 0),
        origin=scope.get("origin"),
        doc_hash=str(scope.get("doc_hash", "")),
        commit_reason=str(msg.get("commit_reason", "change")),
        ts_ms=int(msg.get("ts", 0) or 0),
    )


def ingest(store, policy: Policy, msg: dict[str, Any]) -> str:
    """담았으면 'sealed', 버렸으면 'drop:<reason>'."""
    ev = event_from_message(msg)
    decision = policy.decide(ev)
    if not decision.capture:
        return f"drop:{decision.reason}"
    store.seal({
        "adapter": ev.adapter,
        "scope": {"origin": ev.origin, "doc_hash": ev.doc_hash, "field_path": ev.field_name},
        "field": {"name": ev.field_name, "type": ev.field_type},
        "value": decision.value,          # 레드액션 적용된 값
        "commit_reason": ev.commit_reason,
        "ts": ev.ts_ms,
    })
    return "sealed"
