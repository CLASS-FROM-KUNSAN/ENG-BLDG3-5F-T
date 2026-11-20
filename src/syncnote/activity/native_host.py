"""네이티브 메시징 호스트 — 브라우저 확장(A/B 어댑터)의 받는 쪽.

확장이 보낸 필드 커밋을 길이-프레임으로 읽어 정책·봉인 파이프라인에 넘깁니다.
네트워크를 타지 않습니다(stdio 뿐). 응답에는 **민감 데이터를 절대 넣지 않습니다** —
담김/버림 결과 코드만.

실행은 브라우저가 레지스트리의 호스트 매니페스트(allowed_origins = 그 확장 ID)를
통해 띄웁니다. 등록·확장(JS/MV3)은 다음 증분입니다.
"""

from __future__ import annotations

import sys
from typing import Any

from . import framing


def handle_message(msg: dict[str, Any], store, policy) -> str:
    # 필드 커밋만 처리. 그 밖 모양(프로브 등)은 이번 증분에서 무시.
    if msg.get("v") == 1 and "value" in msg:
        from .pipeline import ingest

        return ingest(store, policy, msg)
    return "ignored"


def main() -> int:
    from .. import config as config_module
    from ..logging_setup import get as get_logger
    from .policy import Policy
    from .store import open_sealed

    logger = get_logger("activity.host")
    cfg = config_module.load(None)
    if not cfg.get("activity.enabled"):
        logger.error("activity.enabled = false — 호스트를 종료합니다.")
        return 1

    try:
        store = open_sealed(cfg)          # 게이트 통과해야 열림. 실패 → GateError
    except Exception as exc:
        logger.error("저장소를 열지 못했습니다(게이트/TPM): %s", exc)
        return 2

    policy = Policy.from_settings(cfg.section("activity"))
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            msg = framing.read_message(stdin)
        except framing.FramingError as exc:
            logger.warning("프레임 오류로 중단: %s", exc)
            break
        if msg is None:
            break
        try:
            outcome = handle_message(msg, store, policy)
        except Exception as exc:
            logger.error("메시지 처리 오류: %s", exc)
            outcome = "error"
        framing.write_message(stdout, {"ok": outcome == "sealed", "outcome": outcome})
    store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
