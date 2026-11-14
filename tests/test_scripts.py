"""Windows 스크립트 정적 검사.

여기 있는 .bat / .vbs / .ps1 은 리눅스 CI 에서 실행할 수 없습니다.
그래서 '돌려 보고 아는' 대신 '글자로 확인할 수 있는 함정' 만 막습니다.
전부 실제로 깨졌던 것들입니다.
"""

from __future__ import annotations

import codecs
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
BATS = sorted(SCRIPTS.glob("*.bat"))


def read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# 1) `r%VAR%` — raw 문자열 접두사 뒤에 따옴표가 없어 SyntaxError 가 납니다.
#    r%REPO%\src 는 파이썬에게 rC:\repo\src 로 전달됩니다.
# ---------------------------------------------------------------------------
def code_lines(text: str) -> str:
    """REM / :: 주석을 뺀 줄만. 함정을 설명하는 주석까지 걸리면 곤란합니다."""
    keep = [ln for ln in text.splitlines()
            if not re.match(r"\s*(REM\b|::)", ln, re.I)]
    return "\n".join(keep)


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_no_raw_prefix_before_a_bat_variable(path: Path):
    text = code_lines(path.read_text(encoding="utf-8"))
    bad = re.findall(r"(?<![A-Za-z0-9_])r%[A-Za-z_][A-Za-z0-9_]*%", text)
    assert not bad, f"{path.name}: r%VAR% 는 파이썬 SyntaxError 입니다 -> {bad}"


def test_the_python_snippet_in_open_data_folder_actually_compiles():
    """실제로 파이썬에 넘어가는 문자열을 뽑아 컴파일해 봅니다."""
    m = re.search(r'"%PY%" -c "([^"]+)"', read("open_data_folder.bat"))
    assert m, "python -c 호출을 찾지 못했습니다"
    compile(m.group(1), "<open_data_folder.bat>", "exec")


# ---------------------------------------------------------------------------
# 2) powershell -Command 안의 %VAR% 는 따옴표로 감싸야 합니다.
#    C:\Users\John Doe\... 가 두 인자로 쪼개집니다.
# ---------------------------------------------------------------------------
PS_PATH_OPTS = ("-OutFile", "-Path", "-DestinationPath", "-LiteralPath", "-FilePath")


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_powershell_path_arguments_are_quoted(path: Path):
    text = code_lines(path.read_text(encoding="utf-8"))
    if "powershell" not in text.lower():
        pytest.skip("powershell 을 쓰지 않습니다")
    bad = []
    for opt in PS_PATH_OPTS:
        # -OutFile %ZIP%  (따옴표 없음) 을 잡습니다.
        bad += re.findall(rf"{opt}\s+(?![\"'])%[A-Za-z_][A-Za-z0-9_]*%\S*", text)
    assert not bad, (
        f"{path.name}: 경로에 공백이 있으면 PowerShell 이 쪼갭니다. "
        f"작은따옴표로 감싸세요 -> {bad}"
    )


# ---------------------------------------------------------------------------
# 3) hidden.vbs 가 기다리지 않으면 작업 스케줄러의 재시작·시간 제한이
#    전부 무용지물이 됩니다.
# ---------------------------------------------------------------------------
def test_hidden_vbs_waits_for_the_child():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    runs = re.findall(r"shell\.Run\s*\(?\s*cmd\s*,\s*0\s*,\s*(True|False)", text)
    assert runs, "shell.Run 호출을 찾지 못했습니다"
    assert all(r == "True" for r in runs), (
        "기다리지 않으면 스케줄러가 곧바로 '성공' 으로 끝내고, "
        "-RestartCount / -ExecutionTimeLimit 가 죽은 설정이 됩니다."
    )


def test_hidden_vbs_runs_the_child_exactly_once():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    assert text.count("shell.Run") == 1, "두 번 실행하면 작업이 두 벌 돕니다"


def test_hidden_vbs_returns_the_child_exit_code():
    text = (SCRIPTS / "hidden.vbs").read_text(encoding="utf-8")
    assert re.search(r"WScript\.Quit\s+rc", text), (
        "종료 코드를 돌려주지 않으면 스케줄러가 실패를 알아보지 못합니다"
    )


# ---------------------------------------------------------------------------
# 4) 폴더 업로드는 root 를 넘겨야 동명 파일이 안 빠집니다.
# ---------------------------------------------------------------------------
def test_manual_upload_passes_a_root_so_same_names_survive():
    src = (Path(__file__).resolve().parents[1]
           / "src/gymhall/cli.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_upload"):src.index("def cmd_config_check")]
    assert "root=" in body, (
        "root 가 없으면 에셋 이름이 파일 이름만으로 정해져 "
        "courseA/자료.md 와 courseB/자료.md 가 충돌합니다"
    )


# ---------------------------------------------------------------------------
# 5) 공통: 모든 .bat 이 _common.bat 을 부르는지(PYTHONPATH/인코딩)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_every_bat_sets_up_the_common_environment(path: Path):
    if path.name == "_common.bat":
        pytest.skip("공통 파일 자신")
    text = path.read_text(encoding="utf-8")
    assert "_common.bat" in text, f"{path.name}: _common.bat 을 부르지 않습니다"


# --- 문서 링크 -----------------------------------------------------
# 마크다운 링크는 어떤 테스트도 안 잡는 부류입니다. 파일 이름을 바꾸거나
# 문서를 옮기면 README 의 링크가 조용히 죽습니다.
def test_every_markdown_link_to_a_repo_file_resolves():
    import re

    root = Path(__file__).resolve().parents[1]
    bad = []
    for md in sorted(root.glob("*.md")) + sorted((root / "docs").glob("*.md")):
        text = md.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?:|#)([^)#\s]+)", text):
            if (md.parent / target).resolve().exists():
                continue
            if (root / target).exists():
                continue
            bad.append(f"{md.relative_to(root)} -> {target}")
    assert not bad, "깨진 문서 링크:\n  " + "\n  ".join(bad)


# ---------------------------------------------------------------------------
# 한글이 든 .ps1 은 UTF-8 BOM 이 있어야 합니다.
#   Windows PowerShell 5.1(강의실 PC 의 'powershell -File') 은 BOM 없는 파일을
#   cp949 로 읽습니다. register_tasks.ps1 이 162줄에서 '문자열 끝이 없습니다'
#   로 멈춰 예약 작업을 아예 못 만들었습니다(재검토에서 재현). CI 의 pwsh(7)
#   는 UTF-8 로 읽어서 통과했습니다.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(SCRIPTS.glob("*.ps1")), ids=lambda p: p.name)
def test_non_ascii_powershell_scripts_have_a_utf8_bom(path: Path):
    data = path.read_bytes()
    if all(b < 128 for b in data):
        return
    assert data.startswith(codecs.BOM_UTF8), f"{path.name}: 한글이 있는데 UTF-8 BOM 이 없습니다"
