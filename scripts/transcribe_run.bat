@echo off
REM 작업 스케줄러가 야간에 부르는 무인 전사. 물어보지 않고 바로 돌립니다.
call "%~dp0_common.bat"
"%PY%" -m gymhall --quiet transcribe queue
exit /b %ERRORLEVEL%
