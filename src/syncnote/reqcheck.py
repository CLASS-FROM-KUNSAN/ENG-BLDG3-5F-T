"""requirements.txt 와 실제 설치된 패키지를 맞춰 봅니다.

    python -m syncnote.reqcheck requirements.txt [--import]     → JSON 한 줄

검사하려는 파이썬(보통 .venv)으로 실행해야 그 환경을 봅니다. 줄마다
  ok        설치됐고 버전 조건도 맞음 (--import 면 실제 import 까지 됨)
  missing   설치 안 됨
  mismatch  설치됐지만 버전 조건(>=, < 등)에 안 맞음
  broken    설치·버전은 맞는데 import 가 안 됨(--import 일 때만). DLL·VC++ 런타임이
            빠졌거나 설치가 반쯤 깨진 경우 — 버전 정보만 보면 '정상' 으로 속습니다.
  skip      이 파이썬·OS 에는 해당 없음(환경 표시 `; python_version < "3.11"` 등)
버전 조건·환경 표시는 pip 와 같은 규칙(packaging)으로 읽습니다.
"""

from __future__ import annotations

import importlib
import json
import sys
from importlib import metadata
from pathlib import Path

try:
    from packaging.requirements import InvalidRequirement, Requirement
except ImportError:  # pragma: no cover - pip 에 들어 있는 것을 씀
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement


def requirement_lines(text: str) -> list[str]:
    """주석·빈 줄·pip 옵션(-r, --index-url 등)을 뺀 요구 사항 줄."""
    out = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        out.append(line)
    return out


def norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def import_names(dist: str) -> list[str]:
    """배포 이름 → import 할 최상위 모듈(faster-whisper → faster_whisper)."""
    try:
        mapping = metadata.packages_distributions()
    except Exception:  # noqa: BLE001
        mapping = {}
    # 패키지에 따라 'PySide6/QtCore' 같은 경로 조각이 섞여 나옵니다 — 모듈 이름만.
    mods = sorted(m for m, ds in mapping.items()
                  if any(norm(d) == norm(dist) for d in ds)
                  and m.isidentifier() and not m.startswith("_"))
    guess = dist.replace("-", "_")
    # 배포 이름과 같은 모듈(PySide6, faster_whisper)을 먼저.
    mods.sort(key=lambda m: norm(m) != norm(guess))
    return mods or [guess]


def try_import(module: str) -> str | None:
    """import 해 보고 실패 이유(없으면 None)."""
    try:
        importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 — DLL load failed(OSError)·ImportError 모두
        return f"{type(exc).__name__}: {exc}"
    return None


def check_line(line: str, imports: bool = False) -> dict:
    try:
        req = Requirement(line)
    except InvalidRequirement as exc:
        return {"line": line, "name": line, "status": "invalid", "installed": None,
                "detail": str(exc)}
    item = {"line": line, "name": req.name, "spec": str(req.specifier), "installed": None}
    if req.marker is not None and not req.marker.evaluate():
        return {**item, "status": "skip", "detail": f"해당 없음({req.marker})"}
    try:
        ver = metadata.version(req.name)
    except metadata.PackageNotFoundError:
        return {**item, "status": "missing", "detail": "설치 안 됨"}
    item["installed"] = ver
    if req.specifier and not req.specifier.contains(ver, prereleases=True):
        return {**item, "status": "mismatch", "detail": f"{ver} 는 {req.specifier} 에 안 맞음"}
    if imports:
        for mod in import_names(req.name)[:3]:
            why = try_import(mod)
            if why:
                return {**item, "status": "broken", "detail": f"import {mod} 실패 — {why}"}
    return {**item, "status": "ok", "detail": ver}


def check(text: str, imports: bool = False) -> list[dict]:
    return [check_line(ln, imports) for ln in requirement_lines(text)]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    imports = "--import" in argv
    rest = [a for a in argv if a != "--import"]
    path = Path(rest[0]) if rest else Path(__file__).resolve().parents[2] / "requirements.txt"
    items = check(path.read_text(encoding="utf-8"), imports)
    print(json.dumps({"python": sys.executable, "items": items}, ensure_ascii=False))
    return 0 if all(i["status"] in ("ok", "skip") for i in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
