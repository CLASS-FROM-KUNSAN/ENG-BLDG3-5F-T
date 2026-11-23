@REM =====================================================================
@REM  Find Python. Every .bat picks Python ONLY here (never call python/py directly).
@REM  Called by _common.bat and activity_host.bat. Prints NOTHING (activity_host
@REM  uses stdout for native-messaging frames). ASCII only: this file may be read
@REM  before or after "chcp 65001" (old cmd mis-splits UTF-8 lines across a code
@REM  page change), so it must not contain Korean.
@REM
@REM  Rule (docs/SETUP.md "Python launch rule"; enforced by tests/test_scripts.py):
@REM    The Python command differs per PC: python, py (launcher), python3.
@REM    Respect the user's install and fall back to the next way when one fails.
@REM    Pick by "it really runs and is >= 3.10", not by "where python" finds it:
@REM    the Microsoft Store app-execution alias python.exe is found by where,
@REM    but prints "Python" and fails (classroom PC: "Python ... failed").
@REM
@REM  Candidates, first that passes wins:
@REM    1) SYNCNOTE_PYTHON  user override (full path or command)
@REM    2) python
@REM    3) py -3            Windows Python launcher
@REM    4) python3
@REM    5) py
@REM  Results (real full path of sys.executable, safe to quote as "%PY%"):
@REM    PYBASE  system Python (used to create .venv); empty if none works
@REM    PYFROM  which candidate found it (for messages)
@REM    PY      .venv python if it runs, else PYBASE
@REM =====================================================================
@set "PYBASE="
@set "PYFROM="
@if defined SYNCNOTE_PYTHON call :try "SYNCNOTE_PYTHON" "%SYNCNOTE_PYTHON%"
@if not defined PYBASE call :try "python" python
@if not defined PYBASE call :try "py -3" py -3
@if not defined PYBASE call :try "python3" python3
@if not defined PYBASE call :try "py" py
@set "PY="
@set "PYCHECK="
@if exist "%REPO%\.venv\Scripts\python.exe" call :check "%REPO%\.venv\Scripts\python.exe"
@if defined PYCHECK set "PY=%PYCHECK%"
@if not defined PY set "PY=%PYBASE%"
@set "PYCHECK="
@exit /b 0

:try
@REM %1 = label, rest = command. On success fills PYBASE and PYFROM.
@set "PYNAME=%~1"
@shift
@call :check %1 %2 %3
@if defined PYCHECK set "PYBASE=%PYCHECK%"
@if defined PYCHECK set "PYFROM=%PYNAME%"
@set "PYCHECK="
@set "PYNAME="
@exit /b 0

:check
@REM Run it; if >= 3.10, put sys.executable into PYCHECK, else leave it empty.
@REM The leading "call" keeps a quoted path intact inside for /f.
@set "PYCHECK="
@for /f "usebackq delims=" %%P in (`call %* -c "import sys;(min(sys.version_info[:2],(3,10))==(3,10)) and print(sys.executable)" 2^>nul`) do @set "PYCHECK=%%P"
@if defined PYCHECK if not exist "%PYCHECK%" set "PYCHECK="
@exit /b 0
