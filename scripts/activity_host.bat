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
REM Execution policy check like every other script (_common.bat), but silent
REM and with stdin closed: stdin/stdout carry the browser protocol.
REM PSModulePath emptied for the call only (see _common.bat).
set "SN_PSMP=%PSModulePath%"
set "PSModulePath="
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0exec_policy.ps1" <nul >nul 2>&1
set "PSModulePath=%SN_PSMP%"
set "SN_PSMP="
REM Python only via _find_python.bat (prints nothing). Rule: docs/SETUP.md
call "%~dp0_find_python.bat"
if not defined PY exit /b 9009
"%PY%" -m syncnote.activity.native_host
