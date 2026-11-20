@echo off
call "%~dp0_common.bat"
REM 인자는 그대로 넘깁니다(시간표 편집기는 --json 으로 불러 결과를 그림).
"%PY%" -m syncnote doctor %*
echo.
pause
