@echo off
REM 작업 스케줄러가 매일 부르는 홈 백업. 설정에서 backup.home.enabled 가 켜져 있어야 올립니다.
call "%~dp0_common.bat"
"%PY%" -m syncnote --quiet backup home
exit /b %ERRORLEVEL%
