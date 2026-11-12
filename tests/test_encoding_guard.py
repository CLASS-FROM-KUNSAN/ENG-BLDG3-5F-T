"""인코딩을 안 적은 호출 찾기.

한글 Windows 에서만 터지는 종류입니다. `open()` 과 `text=True` 는
인코딩을 안 적으면 **로캘**을 씁니다. 리눅스 CI 는 UTF-8 이라 늘
초록이고, 사용자 PC 는 cp949 라 거기서만 죽습니다.

2026-11-13 실기에서 테스트 2건이 이걸로 실패했습니다. 제품 코드는
`ffmpeg_util.run` 과 `watcher` 가 이미 `encoding="utf-8"` 을 적고
있어서 멀쩡했고, 테스트 쪽만 빠져 있었습니다. 테스트가 못 도는 것도
고장입니다 — 그 PC 에서는 아무것도 검증되지 않습니다.

ruff 의 PLW1514 가 `open()` 쪽을 보지만 preview 전용이고
`subprocess(text=True)` 는 안 봅니다. 그래서 여기서 둘 다 봅니다.
정규식이 아니라 AST 로 보므로 문자열이나 주석에 속지 않습니다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted(
    p for d in ("src", "tests") for p in (ROOT / d).rglob("*.py")
)

# 바이너리 모드는 인코딩이 없는 게 맞습니다.
_BINARY = ("rb", "wb", "ab", "r+b", "w+b", "a+b", "br", "bw", "ba")
_TEXTY = {"run", "Popen", "check_output", "call", "check_call"}


def _kw(node: ast.Call, name: str):
    for k in node.keywords:
        if k.arg == name:
            return k.value
    return None


def _is_truthy(node) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _binary_mode(node: ast.Call, *, method: bool) -> bool:
    """바이너리 모드면 인코딩이 없는 게 맞습니다.

    mode 가 몇 번째 인자인지가 두 경우에 다릅니다.
        open(path, "rb")        -> args[1]
        Path(path).open("rb")   -> args[0]
    이걸 섞으면 멀쩡한 바이너리 호출 여섯 개를 전부 위반으로 찍습니다
    (실제로 처음에 그랬습니다).
    """
    mode = _kw(node, "mode")
    if mode is None:
        idx = 0 if method else 1
        if len(node.args) > idx:
            mode = node.args[idx]
    return isinstance(mode, ast.Constant) and str(mode.value) in _BINARY


def _func_name(node: ast.Call) -> tuple[str, str]:
    """(점 앞 이름, 함수 이름). open() 이면 ('', 'open')."""
    f = node.func
    if isinstance(f, ast.Name):
        return "", f.id
    if isinstance(f, ast.Attribute):
        owner = f.value.id if isinstance(f.value, ast.Name) else ""
        return owner, f.attr
    return "", ""


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner, name = _func_name(node)
        # encoding 은 위치 인자로도 넘어갑니다. 그 자리가 셋 다 다릅니다.
        #     open(file, mode, buffering, encoding)   -> args[3]
        #     Path.read_text(encoding, errors)        -> args[0]
        #     Path.write_text(data, encoding, errors) -> args[1]
        # 키워드만 보면 read_text("utf-8") 같은 멀쩡한 코드를 위반으로
        # 찍습니다(실제로 처음에 그랬습니다).
        _ENC_POS = {"read_text": 0, "write_text": 1, "open": 3}
        has_encoding = (
            _kw(node, "encoding") is not None
            or (name in _ENC_POS and len(node.args) > _ENC_POS[name])
        )

        if name in ("open", "read_text", "write_text") and not has_encoding:
            # Store.open / zipfile.ZipFile.open 등은 우리 API 입니다.
            if owner in ("Store", "zipfile", "wave", "os", "gzip", "tarfile"):
                continue
            if name == "open" and _binary_mode(node, method=bool(owner) or
                                                isinstance(node.func, ast.Attribute)):
                continue
            bad.append(f"{path.name}:{node.lineno} {owner or ''}{'.' if owner else ''}{name}()")
            continue

        if owner == "subprocess" and name in _TEXTY and not has_encoding:
            texty = _is_truthy(_kw(node, "text")) or \
                _is_truthy(_kw(node, "universal_newlines"))
            if texty:
                bad.append(f"{path.name}:{node.lineno} subprocess.{name}(text=True)")
    return bad


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_text_call_names_its_encoding(path):
    """로캘에 맡기면 한글 Windows 에서만 깨집니다."""
    bad = _offenders(path)
    assert not bad, (
        "인코딩을 적지 않은 호출입니다. 한글 Windows(cp949)에서만 터집니다:\n  "
        + "\n  ".join(bad)
        + "\n  → encoding=\"utf-8\" 을 적으세요. 외부 프로그램 출력이면 "
        "errors=\"replace\" 도 함께."
    )


def test_the_guard_actually_catches_things(tmp_path):
    """검사기가 아무것도 못 잡으면 위 테스트는 영원히 초록입니다."""
    sample = tmp_path / "x.py"
    sample.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "open('a.txt')\n"
        "Path('b.txt').read_text()\n"
        "Path('c.txt').write_text('x')\n"
        "subprocess.run(['x'], text=True)\n"
        "subprocess.run(['x'], universal_newlines=True)\n"
        "Path('d.txt').open()\n",          # 메서드인데 모드가 없으면 텍스트
        encoding="utf-8",
    )
    assert len(_offenders(sample)) == 6, _offenders(sample)


def test_the_guard_does_not_cry_wolf(tmp_path):
    sample = tmp_path / "y.py"
    sample.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "open('a.bin', 'rb')\n"
        "Path('a.bin').open('rb')\n"       # 메서드는 모드가 첫 인자
        "Path('a.bin').open('wb')\n"
        "open('a.txt', encoding='utf-8')\n"
        "Path('b.txt').read_text(encoding='utf-8')\n"
        "Path('c.txt').write_text('x', encoding='utf-8')\n"
        "Path('d.txt').read_text('utf-8')\n"          # 위치 인자도 인코딩입니다
        "Path('e.txt').write_text('x', 'utf-8')\n"
        "subprocess.run(['x'], text=True, encoding='utf-8')\n"
        "subprocess.run(['x'])\n"            # 바이트 모드는 괜찮습니다
        "Store.open('s.json')\n",            # 우리 API
        encoding="utf-8",
    )
    assert _offenders(sample) == []
