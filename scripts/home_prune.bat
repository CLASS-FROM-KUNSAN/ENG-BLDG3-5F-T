@echo off
REM Daily home cleanup (Task Scheduler). Deletes only files whose backup
REM chain is confirmed on GitHub, and only if backup.home.prune.enabled is on.
call "%~dp0_common.bat"
"%PY%" -m syncnote --quiet backup prune
exit /b %ERRORLEVEL%
