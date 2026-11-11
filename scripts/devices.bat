@echo off
REM 오디오 장치 목록. 여기 나오는 이름을 config.toml 에 적으세요.
call "%~dp0_common.bat"
"%PY%" -m gymhall devices
echo.
pause
