"""테스트 공통 설정.

이 테스트들은 네트워크, 오디오 장치, 브라우저 없이 전부 돌아갑니다.
GitHub API 는 가짜 세션으로, 오디오 장치는 가짜 소스로 대체합니다.

바이트코드 주의
---------------
**PYTHONDONTWRITEBYTECODE=1 로 돌리세요.** 파일 수정 시각은 초 단위라,
편집이 같은 초에 몰리면 파이썬이 묵은 .pyc 를 그대로 씁니다. 그러면
고친 코드가 아니라 옛 코드가 돌고, 테스트가 거짓으로 통과합니다.
2026-11-12 에 실제로 당했습니다 — 경로 테스트 둘이 소스는 맞는데
실패했고, 그 직전에 돌린 변이 테스트 결과도 전부 믿을 수 없었습니다.

    PYTHONDONTWRITEBYTECODE=1 python -m pytest

CI(.github/workflows/tests.yml)와 scripts/_common.bat 에는 이미 켜 뒀습니다.
변이 테스트를 손으로 돌릴 때는 파일을 고친 뒤
    find . -name __pycache__ -type d -exec rm -rf {} +
까지 같이 하세요. conftest.py 에서 sys.dont_write_bytecode 를 켜는 것은
**늦습니다** — 그 시점에는 이미 .pyc 를 읽고 난 뒤입니다.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


@pytest.fixture(scope="session", autouse=True)
def _quiet_logging():
    """로그가 테스트 출력을 덮지 않게 파일로만 보냅니다."""
    from syncnote.logging_setup import setup

    setup(Path(tempfile.mkdtemp()) / "logs", level="WARNING", console=False)


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def example_config_text() -> str:
    return (REPO / "config.example.toml").read_text(encoding="utf-8")


@pytest.fixture
def config_file(tmp: Path, example_config_text: str):
    """config.example.toml 을 바탕으로 설정 파일을 만듭니다.

    edits 는 (찾을 문자열, 바꿀 문자열) 쌍의 목록입니다. 찾을 문자열이
    예시 파일에 없으면 바로 실패합니다 — 예시 파일이 바뀌었는데 테스트가
    조용히 통과하는 상황을 막기 위해서입니다.
    """

    def _make(edits: list[tuple[str, str]] | None = None) -> Path:
        text = example_config_text
        text = text.replace('username = ""', 'username = "TESTID"', 1)
        text = text.replace('data_dir = ""', f'data_dir = "{tmp.as_posix()}/data"')
        text = text.replace('machine_label = ""', 'machine_label = "testpc"')
        for old_s, new_s in edits or []:
            assert old_s in text, f"치환 대상을 찾지 못했습니다: {old_s!r}"
            text = text.replace(old_s, new_s, 1)
        path = tmp / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return _make
