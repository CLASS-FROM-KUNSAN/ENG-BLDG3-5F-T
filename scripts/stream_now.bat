@echo off
REM =====================================================================
REM  Start live audio streaming (inside the Tailscale network).
REM  The link is posted as a GitHub issue. Settings: [stream].
REM    stream_now.bat                     as configured
REM    stream_now.bat -l <name> -m 100    name and length
REM    stream_now.bat --source mic        microphone
REM    stream_now.bat --no-issue          no issue
REM  Ctrl+C here stops it, turns tailscale serve off and closes the issue.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m syncnote stream start %*
echo.
pause
