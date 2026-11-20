@echo off
REM 작업 스케줄러가 매일 부르는 홈 정리. GitHub 에 올라간 사슬이 확인된 파일만 지웁니다.
REM 설정에서 backup.home.prune.enabled 가 켜져 있어야 지웁니다.
call "%~dp0_common.bat"
"%PY%" -m syncnote --quiet backup prune
exit /b %ERRORLEVEL%
