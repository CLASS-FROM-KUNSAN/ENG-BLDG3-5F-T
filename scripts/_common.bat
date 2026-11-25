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
REM  Switch the console to UTF-8, so msg\*.txt shown by _say.bat
REM  (and Python output) render correctly.
REM ---------------------------------------------------------------------
chcp 65001 >nul 2>&1

REM ---------------------------------------------------------------------
REM  PowerShell execution policy (docs/SCRIPTS.md "Execution policy").
REM  Regular step of EVERY script, before anything else runs: exec_policy.ps1
REM  reads Get-ExecutionPolicy -List and, if .ps1 files cannot run
REM  (Restricted / AllSigned / Undefined), sets RemoteSigned for the current
REM  user permanently (no admin needed; also LocalMachine when elevated) and
REM  unblocks downloaded .ps1 here (MOTW). Group Policy cannot be changed: it
REM  only reports. It always exits 0: never stops a script or a recording.
REM  Started with -ExecutionPolicy Bypass so it runs even while blocked.
REM  SN_POLICY_SHOW=1 (setup.bat, doctor.bat) also prints the current policy.
REM  Last step: Python and messages above do not depend on it.
REM  PSModulePath is emptied for this one call: started from a PowerShell 7
REM  window it points at PowerShell 7 modules, and Windows PowerShell 5.1 then
REM  fails to load Get-ExecutionPolicy (seen on the test PC). Empty, 5.1
REM  rebuilds its own default. It is put back right after.
REM ---------------------------------------------------------------------
set "SN_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%SN_PS%" set "SN_PS=powershell"
set "SN_PS_SHOW="
if "%SN_POLICY_SHOW%"=="1" set "SN_PS_SHOW=-Show"
set "SN_PSMP=%PSModulePath%"
set "PSModulePath="
"%SN_PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0exec_policy.ps1" %SN_PS_SHOW% <nul 2>nul
set "PSModulePath=%SN_PSMP%"
set "SN_PSMP="
