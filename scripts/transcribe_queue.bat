@echo off
REM =====================================================================
REM  밀린 전사를 한 번에 처리합니다.
REM  발열과 배터리를 많이 쓰므로 충전기에 꽂고 돌리세요.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m gymhall transcribe queue --show
echo.
set /p GO="지금 전사할까요? (Y/N): "
if /i not "%GO%"=="Y" exit /b 0
"%PY%" -m gymhall transcribe queue %*
echo.
pause
