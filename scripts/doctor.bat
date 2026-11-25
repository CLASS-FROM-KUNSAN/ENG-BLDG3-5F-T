@echo off
REM Environment check. Arguments are passed through (editor uses --json).
REM Show the PowerShell execution policy check (done in _common.bat).
set "SN_POLICY_SHOW=1"
call "%~dp0_common.bat"
"%PY%" -m syncnote doctor %*
REM Then register missing Task Scheduler tasks (UAC if access is denied).
REM Not with --json: the timetable editor parses that output (it registers
REM on save instead).
if /i not "%~1"=="--json" (
    echo.
    call "%~dp0_say.bat" doctor_tasks
    "%PY%" -m syncnote tasks ensure
)
echo.
pause
