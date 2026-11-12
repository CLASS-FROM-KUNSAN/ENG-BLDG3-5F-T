@echo off
call "%~dp0_common.bat"
"%PY%" -m gymhall record schedule --show
echo.
pause
