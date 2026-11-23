@echo off
REM Environment check. Arguments are passed through (editor uses --json).
call "%~dp0_common.bat"
"%PY%" -m syncnote doctor %*
echo.
pause
