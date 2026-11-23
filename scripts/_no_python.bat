@echo off
REM What PY points to when no Python ran at all (set by _common.bat).
REM Called as "%PY%" -m ..., so arguments are ignored: show guidance, then fail.
call "%~dp0_say.bat" no_python
exit /b 9009
