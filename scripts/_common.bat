REM =====================================================================
REM  Common environment. Other .bat files "call" this. Do not run it directly.
REM  ASCII only (see docs/SCRIPTS.md): Korean here broke cmd after the call to
REM  _find_python.bat returned (fragments of comments ran as commands).
REM =====================================================================
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
REM Do not write .pyc: mtimes have 1-second resolution, so right after an update
REM a stale bytecode file can be used and fixed code silently does not run.
set "PYTHONDONTWRITEBYTECODE=1"

REM Repo root = parent of this scripts folder
set "REPO=%~dp0.."
pushd "%REPO%" >nul
set "REPO=%CD%"
popd >nul

set "PYTHONPATH=%REPO%\src"

REM Python is chosen ONLY by _find_python.bat (docs/SETUP.md "Python launch rule"):
REM by "it really runs and is >= 3.10", trying
REM   SYNCNOTE_PYTHON -> python -> py -3 -> python3 -> py
REM The Microsoft Store alias python.exe prints "Python" and fails; it is skipped.
REM   PY     = full path of the Python to use (.venv if it runs, else system)
REM   PYBASE = system Python (to create .venv), PYFROM = which way found it
call "%~dp0_find_python.bat"
REM None at all: every script that runs "%PY%" shows the same guidance.
if not defined PY set "PY=%~dp0_no_python.bat"

REM Bundled ffmpeg goes on PATH if present
if exist "%REPO%\tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%REPO%\tools\ffmpeg\bin;%PATH%"

REM Fix the config.toml location (Task Scheduler may start in another folder).
REM Keep it if already set: the timetable editor runs doctor/probe with the file
REM it opened, and swapping in the repo config.toml would check the wrong file.
if not defined SYNCNOTE_CONFIG if exist "%REPO%\config.toml" set "SYNCNOTE_CONFIG=%REPO%\config.toml"

REM ---------------------------------------------------------------------
REM  Switch the console to UTF-8 last, so msg\*.txt shown by _say.bat
REM  (and Python output) render correctly. Nothing after this line.
REM ---------------------------------------------------------------------
chcp 65001 >nul 2>&1
