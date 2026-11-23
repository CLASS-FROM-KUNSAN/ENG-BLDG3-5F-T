@echo off
REM =====================================================================
REM  Start recording right now.
REM  Closing this window or pressing Ctrl+C finishes the file safely.
REM  A desktop shortcut to this file is handy.
REM =====================================================================
call "%~dp0_common.bat"
call "%~dp0_say.bat" rn_start
echo.
"%PY%" -m syncnote record start %*
echo.
pause
