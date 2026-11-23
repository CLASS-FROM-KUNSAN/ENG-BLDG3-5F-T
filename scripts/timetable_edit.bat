@echo off
call "%~dp0_common.bat"
REM  시간표 편집기(GUI). 저장하면 config.toml 의 [[recording.timetable]] 이 바뀝니다.
REM  PySide6 가 없으면 이유를 알려 주고, 원하면 그 자리에서 설치합니다.
REM  주의: 아래 if 블록 안의 echo 에는 괄호를 쓰지 마세요. cmd 가 블록 끝으로 읽습니다.
REM  파이썬이 어디에도 없으면 PySide6 를 묻기 전에 그 안내부터 — 설치해도 소용없습니다.
if "%PY%"=="%~dp0_no_python.bat" (call "%PY%" & goto :done)
"%PY%" -c "import PySide6.QtWidgets, PySide6.QtUiTools" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  시간표 편집기에는 PySide6 가 필요한데 아직 설치되어 있지 않습니다.
    echo  설치 명령: "%PY%" -m pip install PySide6
    echo  scripts\setup.bat 을 다시 돌려도 함께 설치됩니다.
    echo.
    choice /c YN /m " 지금 설치할까요"
    if errorlevel 2 goto :done
    "%PY%" -m pip install PySide6
    if errorlevel 1 goto :failed
)
"%PY%" -m syncnote timetable edit
if errorlevel 1 goto :done
exit /b 0

:failed
echo.
echo  PySide6 설치에 실패했습니다. 위의 pip 오류를 확인하세요.
:done
pause