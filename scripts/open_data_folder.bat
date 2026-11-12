@echo off
REM 녹화/자료가 저장된 폴더를 엽니다.
call "%~dp0_common.bat"
REM _common.bat 이 PYTHONPATH 를 이미 잡아 줍니다. 여기서 sys.path 를
REM 또 건드리면 r%REPO%\src 가 rC:\repo\src 로 펼쳐져 SyntaxError 입니다
REM (raw 문자열 접두사 r 뒤에는 따옴표가 와야 합니다).
for /f "usebackq delims=" %%P in (`"%PY%" -c "from gymhall import config;print(config.load().data_dir)"`) do set "DATA=%%P"
if not defined DATA (
    echo 설정을 읽지 못했습니다. doctor.bat 을 먼저 돌려 보세요.
    pause
    exit /b 1
)
if exist "%DATA%" (start "" explorer "%DATA%") else (echo 폴더가 아직 없습니다: %DATA% & pause)
