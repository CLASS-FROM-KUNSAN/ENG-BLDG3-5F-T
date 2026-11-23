@echo off
REM =====================================================================
REM  Audio-only recording (terminal recorder: FFmpeg / SoX).
REM  Settings: [recording.terminal] in config.toml.
REM    record_audio.bat                    once, as configured
REM    record_audio.bat -t 3000 -f mp3     50 minutes, mp3
REM    record_audio.bat --vad --loop       one file per sound (SoX)
REM  For Task Scheduler: no pause at the end. To run it hidden at a fixed
REM  time use register_tasks.ps1 -AudioAt 09:00. File names carry date
REM  and time, so daily runs never overwrite each other.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m syncnote audio record %*
exit /b %ERRORLEVEL%
