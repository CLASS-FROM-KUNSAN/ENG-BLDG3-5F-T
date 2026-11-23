@echo off
REM Audio device list. Put the names shown here into config.toml.
REM Arguments are passed through (the timetable editor uses --json).
call "%~dp0_common.bat"
"%PY%" -m syncnote devices %*
echo.
pause
