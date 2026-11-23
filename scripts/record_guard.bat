@echo off
REM Recording guard: while not recording, every few minutes check the
REM recordings, join pieces of interrupted ones, rebuild broken files and
REM upload what is missing ([recording.guard]).
call "%~dp0_common.bat"
"%PY%" -m syncnote record guard %*
exit /b %ERRORLEVEL%
