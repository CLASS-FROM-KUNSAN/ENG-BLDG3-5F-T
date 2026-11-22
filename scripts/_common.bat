REM =====================================================================
REM  공통 환경 설정. 다른 .bat 파일들이 이 파일을 call 합니다.
REM  직접 실행하지 마세요.
REM =====================================================================
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
REM .pyc 를 남기지 않습니다. 파일 수정 시각이 초 단위라, 레포를 업데이트한
REM 직후처럼 편집이 같은 초에 몰리면 묵은 바이트코드가 그대로 쓰입니다.
REM 고친 코드가 안 도는데 이유를 알 수 없는 상태가 됩니다.
set "PYTHONDONTWRITEBYTECODE=1"

REM 레포 루트 = 이 파일이 있는 scripts 폴더의 상위
set "REPO=%~dp0.."
pushd "%REPO%" >nul
set "REPO=%CD%"
popd >nul

set "PYTHONPATH=%REPO%\src"

REM 가상환경이 있으면 그걸 씁니다.
if exist "%REPO%\.venv\Scripts\python.exe" (
    set "PY=%REPO%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM 동봉 ffmpeg 가 있으면 PATH 에 추가
if exist "%REPO%\tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%REPO%\tools\ffmpeg\bin;%PATH%"

REM config.toml 위치를 명시 (작업 스케줄러는 작업 폴더가 엉뚱할 수 있음)
REM 이미 정해져 있으면 그대로 둡니다 — 시간표 편집기가 연 설정으로 doctor·probe 를
REM 돌릴 때 레포의 config.toml 로 바꿔치기되면 엉뚱한 설정을 점검합니다.
if not defined SYNCNOTE_CONFIG if exist "%REPO%\config.toml" set "SYNCNOTE_CONFIG=%REPO%\config.toml"

REM ---------------------------------------------------------------------
REM  코드 페이지(UTF-8) 전환은 반드시 이 파일의 맨 끝에 둡니다.
REM  예전 cmd(강의실 PC)는 배치 파일을 읽는 도중 chcp 로 코드 페이지가 바뀌면
REM  그 뒤 줄의 한글(UTF-8)을 엉뚱한 곳에서 잘라, 주석 조각을 명령으로 실행했습니다:
REM    '바이트코드가' is not recognized as an internal or external command
REM  그래서 이 줄 뒤에는 아무것도(특히 한글을) 두지 않습니다(tests/test_scripts.py 가 막음).
REM ---------------------------------------------------------------------
chcp 65001 >nul 2>&1
