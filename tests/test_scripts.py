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
           / "src/syncnote/cli.py").read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# 6) 줄바꿈이 LF 뿐인 .bat — cmd 가 한글(UTF-8) 줄 근처에서 줄을 잘못 잘라
#    'M' 은(는) 내부 또는 외부 명령... 처럼 엉뚱한 조각을 실행합니다.
#    .gitattributes 는 체크아웃 때만 CRLF 로 바꿔 주므로, 새로 만든 파일은
#    작업 폴더에서 LF 인 채로 실행될 수 있습니다(timetable_edit.bat 에서 실제로 남).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_bat_uses_crlf_only(path: Path):
    raw = path.read_bytes()
    lone_lf = raw.replace(b"\r\n", b"").count(b"\n")
    assert lone_lf == 0, f"{path.name}: CRLF 가 아닌 줄 {lone_lf}개 — cmd 가 줄을 잘못 읽습니다"


# ---------------------------------------------------------------------------
# 7) 괄호 블록 안 echo 의 맨 ')' — 블록을 거기서 닫아 버려 cmd 가
#    '... was unexpected at this time.' 으로 멈춥니다. install_ffmpeg.bat 의
#    내려받기 실패 안내('1) ... 2) ...')가 이래서 한 번도 보이지 않았습니다(이슈 #59).
#    들여쓴 echo 는 블록 안이라고 보고 ^) 로 쓰게 합니다.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_no_unescaped_close_paren_in_block_echo(path: Path):
    bad = [f"{n}: {ln.strip()}" for n, ln in enumerate(code_lines(read(path.name)).splitlines(), 1)
           if re.match(r"\s+echo\b", ln, re.I) and re.search(r"(?<!\^)\)", ln)]
    assert not bad, f"{path.name}: 블록 안 echo 의 ')' 는 ^) 로 —\n" + "\n".join(bad)


# ---------------------------------------------------------------------------
# 8) ffmpeg 는 winget 으로 먼저 설치하고, 동의 질문에서 멈추지 않아야 합니다(이슈 #59).
# ---------------------------------------------------------------------------
def test_install_ffmpeg_uses_winget_without_prompts_then_falls_back_to_zip():
    text = code_lines(read("install_ffmpeg.bat"))
    line = next(ln for ln in text.splitlines() if "winget install" in ln)
    assert line.strip().lower().startswith("call ")        # .bat 별칭이어도 돌아오게
    for opt in ("--id Gyan.FFmpeg", "-e", "--source winget", "--accept-source-agreements",
                "--accept-package-agreements", "--disable-interactivity"):
        assert opt in line, opt
    # 결과는 PATH(이 창엔 반영 안 됨)가 아니라 winget 이 만든 파일로 확인
    assert r"WinGet\Links\ffmpeg.exe" in text and "Gyan.FFmpeg*" in text
    # 찾기 -> pip -> winget -> 릴리스 목록(롤백), 파이썬조차 없을 때만 PowerShell zip
    # 파이썬이 있으면 --auto 가 전부(찾기·zip·pip·winget·릴리스) 맡고, 없을 때만 bat 의 길
    assert text.index("ffmpeg_fetch --auto") < text.index(":nopython") < text.index(
        "winget install")
    assert (text.index("winget install") < text.index("syncnote.ffmpeg_fetch --dest")
            < text.index("Invoke-WebRequest"))
    assert "errorlevel 9009" in text
    # setup.bat 은 ffmpeg 가 없으면 이 스크립트를 부릅니다(그래서 winget 도 같이 탐)
    assert "install_ffmpeg.bat" in read("setup.bat")


# ---------------------------------------------------------------------------
# 9) chcp 뒤의 한글 — 예전 cmd(강의실 PC)는 배치 파일을 읽는 도중 코드 페이지가 바뀌면
#    그 뒤 줄의 UTF-8 을 엉뚱한 곳에서 잘라, 주석 조각을 명령으로 실행했습니다:
#      '바이트코드가' is not recognized as an internal or external command
#    (_common.bat 9·33번째 줄, 둘 다 chcp 65001 뒤의 한글 REM). 최신 Windows 11 의 cmd
#    에서는 재현되지 않아 글자로 막습니다: chcp 줄 뒤에는 ASCII 만.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_nothing_but_ascii_after_chcp(path: Path):
    lines = path.read_bytes().split(b"\r\n")
    at = next((i for i, ln in enumerate(lines)
               if re.match(rb"\s*@?chcp\b", ln, re.I)), None)
    if at is None:
        return
    bad = [i + 1 for i, ln in enumerate(lines[at + 1:], start=at + 1) if any(b > 127 for b in ln)]
    assert not bad, f"{path.name}: chcp(줄 {at + 1}) 뒤에 한글이 있는 줄 {bad} — chcp 를 맨 끝으로"


# ---------------------------------------------------------------------------
# 파이썬 실행 규칙(docs/SETUP.md '파이썬 실행 규칙'): 파이썬은 _find_python 한 곳에서만 고릅니다.
#   강의실 PC: setup.bat 이 `where python` 으로 확인하고 `python -m venv` 를 불렀는데,
#   잡힌 것은 Microsoft Store 의 가짜 python.exe 라 'Python' 만 찍고 실패했습니다.
#   기기마다 python / py / python3 가 다르므로 직접 부르지 말고 %PY% / $PY 만 씁니다.
# ---------------------------------------------------------------------------
# 명령 자리(줄 처음·( & | 뒤) · where/command -v · 'PY=python' 처럼 이름을 넣는 것까지
_DIRECT_PY = re.compile(r"(?:^|[(&|]\s*|\bwhere\s+|command\s+-v\s+|=\s*)(python3?|py)(?:\.exe)?"
                        r"(?=\s|$|\)|\")",
                        re.I | re.M)
FINDERS = {"_find_python.bat", "_find_python.sh"}


@pytest.mark.parametrize("path", sorted(SCRIPTS.glob("*.bat")) + sorted(SCRIPTS.glob("*.sh"))
                         + sorted(SCRIPTS.glob("*.ps1")), ids=lambda p: p.name)
def test_scripts_do_not_launch_python_directly(path: Path):
    if path.name in FINDERS:
        return
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".bat":
        text = code_lines(text)
    else:
        text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    bad = [m.group(0).strip() for m in _DIRECT_PY.finditer(text)]
    assert not bad, (f"{path.name}: 파이썬을 직접 부릅니다 {bad} — %PY%/$PY 를 쓰세요"
                     "(파이썬은 _find_python 에서만 고름. docs/SETUP.md '파이썬 실행 규칙')")


def test_find_python_tries_every_way_and_is_ascii():
    raw = (SCRIPTS / "_find_python.bat").read_bytes()
    assert all(b < 128 for b in raw), "_find_python.bat 은 ASCII 만(chcp 앞뒤 어디서 읽혀도 안전)"
    text = raw.decode("ascii")
    order = [text.index(f"call :try {c}") for c in
             ('"SYNCNOTE_PYTHON"', '"python" python', '"py -3" py -3', '"python3" python3',
              '"py" py')]
    assert order == sorted(order), "후보 순서: SYNCNOTE_PYTHON -> python -> py -3 -> python3 -> py"
    assert "sys.executable" in text and "(3,10)" in text      # '실제로 실행되고 3.10 이상'
    sh = (SCRIPTS / "_find_python.sh").read_text(encoding="utf-8")
    assert '"${SYNCNOTE_PYTHON:-}" python3 python py' in sh


def test_every_launcher_uses_the_finder():
    assert 'call "%~dp0_find_python.bat"' in read("_common.bat")
    assert 'call "%~dp0_find_python.bat"' in read("activity_host.bat")
    for sh in SCRIPTS.glob("*.sh"):
        if sh.name in FINDERS:
            continue
        t = sh.read_text(encoding="utf-8")
        if "$PY" in t:
            assert "_find_python.sh" in t, f"{sh.name}: $PY 를 쓰면 _find_python.sh 로 고르세요"
    setup = read("setup.bat")
    assert "where python" not in setup and '"%PYBASE%" -m venv' in setup


def test_python_code_launches_its_own_interpreter():
    """파이썬 코드가 다른 파이썬을 띄울 때는 sys.executable — "python" 이라는 이름이 아니라."""
    src = Path(__file__).resolve().parents[1] / "src" / "syncnote"
    pat = re.compile(r"""\[\s*["'](python3?|py)(?:\.exe)?["']\s*,""")
    bad = [f"{p.relative_to(src)}:{i}" for p in src.rglob("*.py")
           for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if pat.search(ln)]
    assert not bad, f"이름으로 파이썬을 띄움: {bad} — sys.executable 을 쓰세요"


# ---------------------------------------------------------------------------
# .bat 은 ASCII 만(docs/SCRIPTS.md '규칙 1'). cmd 는 배치 파일의 한글(여러 바이트 글자) 때문에
# 줄 위치를 잘못 계산해 주석 조각을 명령으로 실행합니다 — 2026-11-23 실기에서
# '━硫?臾듭?'은(는) 내부 또는 외부 명령... 으로 setup 이 깨졌고, 한글 주석이 있는 _common.bat 이
# _find_python.bat 을 call 하고 돌아올 때 cp949·UTF-8 창 모두에서 재현됐습니다.
# 한글 안내는 scripts/msg/*.txt 에 두고 _say.bat(type)으로 보여 줍니다.
# ---------------------------------------------------------------------------
MSG = SCRIPTS / "msg"


@pytest.mark.parametrize("path", BATS, ids=lambda p: p.name)
def test_bat_files_are_ascii_only(path: Path):
    raw = path.read_bytes()
    bad = [i for i, ln in enumerate(raw.split(b"\r\n"), 1) if any(b > 127 for b in ln)]
    assert not bad, (f"{path.name}: ASCII 가 아닌 줄 {bad[:10]} — 주석은 영어로, 한글 안내는 "
                     "scripts/msg/<이름>.txt 에 두고 call \"%~dp0_say.bat\" <이름> 으로")


def test_every_message_the_scripts_say_exists():
    used = {(m, p.name) for p in BATS
            for m in re.findall(r'_say\.bat"\s+([A-Za-z0-9_]+)', p.read_text(encoding="ascii"))}
    missing = sorted(f"{p}: {m}" for m, p in used if not (MSG / f"{m}.txt").exists())
    assert used and not missing, f"없는 메시지 파일: {missing}"


@pytest.mark.parametrize("path", sorted(MSG.glob("*.txt")), ids=lambda p: p.name)
def test_message_files_are_utf8_without_bom(path: Path):
    raw = path.read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8), f"{path.name}: BOM 이 있으면 화면에 깨진 글자가 찍힘"
    raw.decode("utf-8")                                   # UTF-8 이 아니면 여기서 실패
