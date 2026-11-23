@echo off
REM Collect eClass materials and upload them to releases.
REM Task Scheduler runs this periodically.
call "%~dp0_common.bat"
"%PY%" -m syncnote eclass sync %*
exit /b %ERRORLEVEL%
