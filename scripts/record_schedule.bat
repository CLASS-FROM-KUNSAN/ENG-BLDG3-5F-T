@echo off
REM Wait and record automatically at class times (timetable).
REM Register it 'at log on' in Task Scheduler to keep it running.
call "%~dp0_common.bat"
"%PY%" -m syncnote record schedule %*
exit /b %ERRORLEVEL%
