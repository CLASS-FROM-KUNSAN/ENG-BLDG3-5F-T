@echo off
call "%~dp0_common.bat"
"%PY%" -m syncnote doctor
echo.
pause
