@echo off
call "%~dp0_common.bat"
"%PY%" -m syncnote record schedule --show
echo.
pause
