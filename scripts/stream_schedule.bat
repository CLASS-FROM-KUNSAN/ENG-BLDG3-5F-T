@echo off
REM =====================================================================
REM  Wait for the timetable ([[recording.timetable]]) and stream each
REM  class. A class's end time is the stream's end time.
REM  For Task Scheduler: register_tasks.ps1 -Stream starts it at log on.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m syncnote stream schedule %*
exit /b %ERRORLEVEL%
