@echo off
REM Record automatically when a configured program (Zoom, ...) starts.
call "%~dp0_common.bat"
"%PY%" -m syncnote record watch %*
exit /b %ERRORLEVEL%
