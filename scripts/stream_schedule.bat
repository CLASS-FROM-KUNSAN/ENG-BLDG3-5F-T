@echo off
REM =====================================================================
REM  시간표([[recording.timetable]])대로 기다렸다가 수업마다 송출합니다.
REM  수업이 끝나는 시각이 송출이 끝나는 시각입니다(시간표가 최우선).
REM  작업 스케줄러용: register_tasks.ps1 -Stream 으로 로그온 시 자동 시작.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m gymhall stream schedule %*
exit /b %ERRORLEVEL%
