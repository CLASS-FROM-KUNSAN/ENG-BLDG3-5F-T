@echo off
REM 오디오 장치 목록. 여기 나오는 이름을 config.toml 에 적으세요.
call "%~dp0_common.bat"
REM 인자는 그대로 넘깁니다(시간표 편집기는 --json 으로 불러 결과를 그림).
"%PY%" -m syncnote devices %*
echo.
pause
