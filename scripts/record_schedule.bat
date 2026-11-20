@echo off
REM 시간표를 보고 대기하다가 수업 시간에 자동으로 녹화합니다.
REM 작업 스케줄러에 "로그온 시" 로 걸어 두면 계속 돌면서 알아서 녹화합니다.
call "%~dp0_common.bat"
"%PY%" -m syncnote record schedule %*
exit /b %ERRORLEVEL%
