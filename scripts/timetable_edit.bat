@echo off
call "%~dp0_common.bat"
REM  Timetable editor (GUI). Saving changes [[recording.timetable]] in config.toml.
REM  If PySide6 is missing, explain why and offer to install it right here.
REM  If no Python runs at all, show that guidance first (installing would not help).
if "%PY%"=="%~dp0_no_python.bat" (call "%PY%" & goto :done)
"%PY%" -c "import PySide6.QtWidgets, PySide6.QtUiTools" >nul 2>&1
if errorlevel 1 (
    echo.
    call "%~dp0_say.bat" tt_need_pyside
    echo "%PY%" -m pip install PySide6
    echo.
    call "%~dp0_say.bat" tt_install_now
    choice /c YN
    if errorlevel 2 goto :done
    "%PY%" -m pip install PySide6
    if errorlevel 1 goto :failed
)
"%PY%" -m syncnote timetable edit
if errorlevel 1 goto :done
exit /b 0

:failed
echo.
call "%~dp0_say.bat" tt_install_failed
:done
pause
