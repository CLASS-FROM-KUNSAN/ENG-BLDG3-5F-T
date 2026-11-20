@echo off
REM Zoom 등 지정한 프로그램이 뜨면 자동으로 녹화합니다.
call "%~dp0_common.bat"
"%PY%" -m syncnote record watch %*
exit /b %ERRORLEVEL%
