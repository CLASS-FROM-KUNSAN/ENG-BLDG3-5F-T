@echo off
REM =====================================================================
REM  최초 1회 설치.  더블클릭하세요.
REM =====================================================================
call "%~dp0_common.bat"
cd /d "%REPO%"

echo ======================================================
echo  Sync Note 설치
echo ======================================================
echo.

REM 파이썬은 _common.bat(_find_python.bat)이 '실제로 실행되는지' 로 골라 둡니다.
REM PATH 에 '있는지' 만 보지 않습니다 — Microsoft Store 의 가짜 python.exe(앱 실행 별칭)도 그 검사를 통과합니다.
if not defined PYBASE (
    call "%~dp0_no_python.bat"
    pause & exit /b 1
)
echo 파이썬: %PYBASE%  [%PYFROM% 으로 찾음]

echo [1/6] 가상환경 만드는 중...
REM 있는데 실행되지 않는 .venv(다른 PC 에서 복사됨, 만든 파이썬을 지움 등)는 옆으로 치우고
REM 새로 만듭니다. 지우지 않고 옮기기만 하므로 필요하면 되돌릴 수 있습니다.
set "BROKEN=.venv.broken-%RANDOM%"
set "VENV_OK=0"
if exist "%REPO%\.venv\Scripts\python.exe" "%REPO%\.venv\Scripts\python.exe" -c "import sys" >nul 2>&1 && set "VENV_OK=1"
if exist "%REPO%\.venv" if "%VENV_OK%"=="0" (
    echo       기존 .venv 가 실행되지 않아 %BROKEN% 로 옮기고 새로 만듭니다.
    move "%REPO%\.venv" "%REPO%\%BROKEN%" >nul
)
if not exist "%REPO%\.venv" (
    "%PYBASE%" -m venv "%REPO%\.venv"
    if errorlevel 1 (
        echo     실패: "%PYBASE%" -m venv
        echo     다른 파이썬을 쓰려면 환경 변수 SYNCNOTE_PYTHON 에 그 경로를 적고 다시 실행하세요.
        pause & exit /b 1
    )
)
set "PY=%REPO%\.venv\Scripts\python.exe"
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (echo     실패: 만든 가상환경의 파이썬이 실행되지 않습니다 & pause & exit /b 1)
echo       완료

echo.
echo [2/6] 파이썬 패키지 설치 중... (몇 분 걸립니다)
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r "%REPO%\requirements.txt"
if errorlevel 1 (echo     실패 & pause & exit /b 1)
echo       완료

echo.
echo [3/6] Playwright 브라우저 설치 중... (약 150MB)
REM 학교망이 HTTPS 를 중간에서 검사하면(self-signed certificate) Windows 인증서
REM 저장소를 Playwright 에도 믿게 하고 다시 받습니다(검증은 끄지 않음).
"%PY%" -m syncnote.playwright_setup
if errorlevel 1 (
    echo     [!] 실패했습니다. Eclass 수집을 쓰지 않는다면 무시해도 됩니다.
)

echo.
echo [4/6] 녹음 도구(ffmpeg, SoX) 확인 중...
where ffmpeg >nul 2>&1
if errorlevel 1 (
    if not exist "%REPO%\tools\ffmpeg\bin\ffmpeg.exe" (
        echo       PATH 에 ffmpeg 가 없습니다. 내려받습니다...
        call "%~dp0install_ffmpeg.bat"
    ) else (
        echo       동봉된 ffmpeg 를 씁니다.
    )
) else (
    echo       이미 설치돼 있습니다.
)
REM SoX: 녹음 방법 사슬(soundcard - ffmpeg - SoX)의 대체 경로이자 소리 감지 녹음 도구(필수).
REM 있으면 그대로, 없으면 winget -> SourceForge 공식 zip(SHA-256 확인) 순으로 설치합니다.
"%PY%" -m syncnote.sox_fetch --dest "%REPO%\tools"
if errorlevel 1 echo       [!] SoX 를 설치하지 못했습니다. 위 이유를 보고 python -m syncnote.sox_fetch 로 다시 하세요.

echo.
echo [5/6] Tailscale 확인 중... (실시간 송출 링크를 엽니다)
set "TS_EXE="
where tailscale >nul 2>&1 && set "TS_EXE=tailscale"
if not defined TS_EXE if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS_EXE=%ProgramFiles%\Tailscale\tailscale.exe"
if defined TS_EXE (
    echo       이미 설치돼 있습니다.
) else (
    where winget >nul 2>&1
    if errorlevel 1 (
        echo     [!] winget 이 없습니다. https://tailscale.com/download 에서 직접 설치하세요.
    ) else (
        echo       winget 으로 설치합니다. 관리자 확인 창이 뜨면 '예' 를 누르세요...
        winget install --id Tailscale.Tailscale -e --silent --accept-package-agreements --accept-source-agreements
        if errorlevel 1 (
            echo     [!] 설치하지 못했습니다. https://tailscale.com/download 에서 직접 설치하세요.
        ) else (
            echo       완료
        )
    )
)

echo.
echo [6/6] 설정 파일 준비
if not exist "%REPO%\config.toml" (
    copy "%REPO%\config.example.toml" "%REPO%\config.toml" >nul
    echo       config.toml 을 만들었습니다.
) else (
    echo       config.toml 이 이미 있습니다. 건드리지 않습니다.
)
REM 마이크 권한이 꺼져 있으면 켭니다(이 PC 전체 항목은 관리자 권한이 있을 때만).
"%PY%" -m syncnote devices --allow-mic

echo.
echo ======================================================
echo  설치 완료. 이제 다음 순서로 진행하세요.
echo ======================================================
echo.
echo  1) config.toml 을 열어 설정을 고치세요.
echo       - github.owner / github.repo
echo       - eclass.username  (학번)
echo       - recording.timetable  (시간표)
echo.
echo  2) 비밀번호와 토큰을 저장하세요.
echo       scripts\set_password.bat
echo.
echo  3) 환경을 점검하세요.
echo       scripts\doctor.bat
echo.
echo  4) 오디오 장치 이름을 확인해 config.toml 에 적으세요.
echo       scripts\devices.bat
echo.
echo  5) 실시간 송출을 쓰려면 Tailscale 에 한 번 로그인하세요.
echo       tailscale up
echo     듣는 사람은 Tailscale 이 없어도 링크(토큰 포함)만 있으면 됩니다.
echo.
pause
