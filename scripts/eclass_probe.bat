@echo off
REM Run this when the eClass page layout changed and collecting fails.
REM A browser window opens; results go to <data>\state\probe.
call "%~dp0_common.bat"
"%PY%" -m syncnote eclass probe --no-headless %*
echo.
pause
