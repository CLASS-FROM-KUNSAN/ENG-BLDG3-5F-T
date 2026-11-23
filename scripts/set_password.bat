@echo off
REM =====================================================================
REM  Store passwords and tokens in Windows Credential Manager.
REM  Input is not shown and never written to a file in plain text.
REM  A number as the first argument skips the menu:
REM    set_password.bat 1     eClass password
REM  (the timetable editor opens it like this when the password is missing).
REM =====================================================================
call "%~dp0_common.bat"

set "CHOICE=%~1"
if not "%CHOICE%"=="" goto :run

call "%~dp0_say.bat" pw_menu
set /p CHOICE=

:run
if "%CHOICE%"=="1" "%PY%" -m syncnote secrets set eclass
if "%CHOICE%"=="2" "%PY%" -m syncnote secrets set github
if "%CHOICE%"=="3" "%PY%" -m syncnote secrets set openai
if "%CHOICE%"=="4" "%PY%" -m syncnote secrets status
echo.
if not "%~1"=="" (
    call "%~dp0_say.bat" pw_close_hint
)
pause
