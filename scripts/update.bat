@echo off
REM Repo auto-update: git pull origin main (src/syncnote/updater.py).
REM Run by the AutoUpdate task every hour; skipped while recording.
REM The pull may replace THIS file. cmd reads a .bat line by line from the
REM same offset, so the python call and the exit are on ONE line: cmd parses
REM the whole line first and never reads this file again afterwards.
call "%~dp0_common.bat"
"%PY%" -m syncnote update %* & exit /b
