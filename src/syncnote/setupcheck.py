"""설치가 '이미 됐는지' 를 한 번에 보는 검사.

    <.venv 의 python> -m syncnote.setupcheck requirements.txt   → JSON 한 줄

시간표 편집기의 설치·점검 창이 setup.bat 을 돌리기 **전**(이미 됐으면 건너뜀)과
**후**(종료 코드가 아니라 실제 상태로 성공 판단)에 씁니다. setup.bat 이 하는 일과
같은 다섯 가지를 봅니다.

  in_venv    이 파이썬이 가상환경인가(.venv)
  reqs_bad   requirements.txt 에서 빠졌거나 버전이 안 맞는 패키지 이름들
  ffmpeg     ffmpeg 경로(없으면 null)
  chromium   Playwright 용 Chromium 이 내려받아져 있나
  tailscale  tailscale 실행 파일 경로(없으면 null) — 실시간 송출이 funnel 로 링크를 엶
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def chromium_installed() -> bool:
    """playwright install chromium 이 받아 둔 브라우저 폴더가 있나."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if custom and custom != "0":
        roots = [Path(custom)]
    elif os.name == "nt":
        roots = [Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                 / "ms-playwright"]
    else:  # pragma: no cover
        roots = [Path.home() / ".cache" / "ms-playwright",
                 Path.home() / "Library" / "Caches" / "ms-playwright"]
    return any(any(r.glob("chromium-*")) for r in roots if r.is_dir())


def report(requirements: Path) -> dict:
    from . import reqcheck

    items = reqcheck.check(Path(requirements).read_text(encoding="utf-8"))
    bad = [i["name"] for i in items if i["status"] not in ("ok", "skip")]
    try:
        from .recorder.ffmpeg_util import find_ffmpeg

        ffmpeg: str | None = find_ffmpeg()
    except Exception:  # noqa: BLE001 — 없음(FFmpegMissing) 포함
        ffmpeg = None
    try:
        from .stream.tailnet import find_exe

        tailscale: str | None = find_exe()
    except Exception:  # noqa: BLE001 — 없음(TailscaleError) 포함
        tailscale = None
    return {
        "python": sys.executable,
        "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
        "reqs_bad": bad,
        "ffmpeg": ffmpeg,
        "chromium": chromium_installed(),
        "tailscale": tailscale,
    }


def missing(rep: dict) -> list[str]:
    """설치가 덜 된 이유(사람이 읽는 말). 비어 있으면 다 됨."""
    out = []
    if not rep.get("in_venv"):
        out.append("가상환경(.venv) 없음")
    if rep.get("reqs_bad"):
        out.append("패키지: " + ", ".join(rep["reqs_bad"]))
    if not rep.get("ffmpeg"):
        out.append("ffmpeg 없음")
    if not rep.get("chromium"):
        out.append("Playwright Chromium 없음")
    if not rep.get("tailscale"):
        out.append("Tailscale 없음")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    req = Path(argv[0]) if argv else Path(__file__).resolve().parents[2] / "requirements.txt"
    rep = report(req)
    print(json.dumps(rep, ensure_ascii=False))
    return 0 if not missing(rep) else 1


if __name__ == "__main__":
    raise SystemExit(main())
