@echo off
REM 녹화 지킴이: 녹화 중이 아닐 때 몇 분마다 녹화 파일을 확인하고, 꺼진 녹화의 조각을
REM 합치고, 망가진 파일을 다시 만들고, 못 올린 것을 올립니다([recording.guard]).
call "%~dp0_common.bat"
"%PY%" -m syncnote record guard %*
exit /b %ERRORLEVEL%
