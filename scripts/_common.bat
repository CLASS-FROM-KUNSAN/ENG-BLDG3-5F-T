REM =====================================================================
REM  공통 환경 설정. 다른 .bat 파일들이 이 파일을 call 합니다.
REM  직접 실행하지 마세요.
REM =====================================================================
chcp 65001 >nul 2>&1
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
if exist "%REPO%\config.toml" set "GYMHALL_CONFIG=%REPO%\config.toml"
