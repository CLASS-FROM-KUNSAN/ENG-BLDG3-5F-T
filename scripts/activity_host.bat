@echo off
REM =====================================================================
REM  Native-messaging host launcher, started by the browser extension.
REM  stdout carries protocol frames only: print NOTHING (so it does not call
REM  _common.bat, which switches the code page). Registered by
REM  scripts\register_host.ps1.
REM =====================================================================
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
if exist "%REPO%\config.toml" set "SYNCNOTE_CONFIG=%REPO%\config.toml"
REM Python only via _find_python.bat (prints nothing). Rule: docs/SETUP.md
call "%~dp0_find_python.bat"
if not defined PY exit /b 9009
"%PY%" -m syncnote.activity.native_host
