@echo off
REM Daily home backup (Task Scheduler). Uploads only when
REM backup.home.enabled is on.
call "%~dp0_common.bat"
"%PY%" -m syncnote --quiet backup home
exit /b %ERRORLEVEL%
