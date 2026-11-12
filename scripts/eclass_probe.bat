@echo off
REM 페이지 구조가 바뀌어 수집이 안 될 때 돌리세요.
REM 브라우저 창이 뜹니다. 결과는 데이터 폴더의 state\probe 아래에 쌓입니다.
call "%~dp0_common.bat"
"%PY%" -m gymhall eclass probe --no-headless %*
echo.
pause
