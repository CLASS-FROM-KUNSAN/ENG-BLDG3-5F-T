@echo off
REM Eclass 자료를 수집하고 릴리스에 올립니다.
REM 작업 스케줄러가 주기적으로 부르는 것이 이 파일입니다.
call "%~dp0_common.bat"
"%PY%" -m gymhall eclass sync %*
exit /b %ERRORLEVEL%
