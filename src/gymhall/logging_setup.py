"""콘솔 + 회전 파일 로깅.

.bat/.vbs 로 백그라운드 실행하면 콘솔 출력이 사라지므로
파일 로그가 사실상 유일한 단서입니다. 반드시 켜 두세요.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

_CONFIGURED = False


class _KSTFormatter(logging.Formatter):
    converter = time.localtime


def setup(
    log_dir: Path,
    level: str = "INFO",
    name: str = "gymhall",
    retention_days: int = 30,
    console: bool = True,
) -> logging.Logger:
    """로거를 초기화합니다. 여러 번 불러도 한 번만 설정됩니다."""
    global _CONFIGURED
    root = logging.getLogger("gymhall")
    if _CONFIGURED:
        return root

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.propagate = False

    fmt = _KSTFormatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_dir / f"{name}.log",
        when="midnight",
        backupCount=max(1, int(retention_days)),
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        # Windows 콘솔에서 한글이 깨지지 않도록
        if os.name == "nt":
            try:
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
        root.addHandler(stream)

    # playwright / urllib3 의 수다를 줄입니다
    for noisy in ("urllib3", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"gymhall.{name}")
