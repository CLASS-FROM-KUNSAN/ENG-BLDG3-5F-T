@echo off
REM Unattended night transcription (Task Scheduler). Runs without asking.
call "%~dp0_common.bat"
"%PY%" -m syncnote --quiet transcribe queue
exit /b %ERRORLEVEL%
