@echo off
REM Step-by-step sound check: is the microphone really coming in?
REM (Korean guide: docs/TROUBLESHOOTING.md "audio_check"). Steps 4-5 play
REM sound on the speakers; pass --no-sound to skip them. Arguments pass through.
call "%~dp0_common.bat"
"%PY%" -m syncnote audio-check %*
echo.
pause
