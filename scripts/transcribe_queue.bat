@echo off
REM =====================================================================
REM  Process the transcription backlog at once.
REM  Heavy on heat and battery: run it on the charger.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m syncnote transcribe queue --show
echo.
call "%~dp0_say.bat" tq_ask
set /p GO=
if /i not "%GO%"=="Y" exit /b 0
"%PY%" -m syncnote transcribe queue %*
echo.
pause
