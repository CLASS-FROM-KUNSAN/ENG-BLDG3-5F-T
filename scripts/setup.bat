@echo off
REM =====================================================================
REM  One-time setup. Double-click it. (Korean guide: docs/SCRIPTS.md)
REM  ASCII only; messages come from msg\setup_*.txt via _say.bat.
REM =====================================================================
call "%~dp0_common.bat"
cd /d "%REPO%"

call "%~dp0_say.bat" setup_title

REM _common.bat already picked Python by "it really runs" (not by where/PATH:
REM the Microsoft Store alias python.exe passes that check but does not run).
if not defined PYBASE (
    call "%~dp0_no_python.bat"
    pause & exit /b 1
)
call "%~dp0_say.bat" setup_python
echo %PYBASE%  [%PYFROM%]

call "%~dp0_say.bat" setup_1
REM A .venv that exists but does not run (copied from another PC, its Python
REM removed, ...) is moved aside, not deleted, and a new one is created.
set "BROKEN=.venv.broken-%RANDOM%"
set "VENV_OK=0"
if exist "%REPO%\.venv\Scripts\python.exe" "%REPO%\.venv\Scripts\python.exe" -c "import sys" >nul 2>&1 && set "VENV_OK=1"
if exist "%REPO%\.venv" if "%VENV_OK%"=="0" (
    call "%~dp0_say.bat" setup_venv_broken
    echo %BROKEN%
    move "%REPO%\.venv" "%REPO%\%BROKEN%" >nul
)
if not exist "%REPO%\.venv" (
    "%PYBASE%" -m venv "%REPO%\.venv"
    if errorlevel 1 (
        call "%~dp0_say.bat" setup_venv_failed
        pause & exit /b 1
    )
)
set "PY=%REPO%\.venv\Scripts\python.exe"
"%PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    call "%~dp0_say.bat" setup_venv_dead
    pause & exit /b 1
)
call "%~dp0_say.bat" done

echo.
call "%~dp0_say.bat" setup_2
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r "%REPO%\requirements.txt"
if errorlevel 1 (
    call "%~dp0_say.bat" failed
    pause & exit /b 1
)
call "%~dp0_say.bat" done

echo.
call "%~dp0_say.bat" setup_3
REM On school networks that inspect HTTPS (self-signed certificate) this makes
REM Playwright also trust the Windows certificate store and retries
REM (verification is never turned off).
"%PY%" -m syncnote.playwright_setup
if errorlevel 1 (
    call "%~dp0_say.bat" setup_3_failed
)

echo.
call "%~dp0_say.bat" setup_4
where ffmpeg >nul 2>&1
if errorlevel 1 (
    if not exist "%REPO%\tools\ffmpeg\bin\ffmpeg.exe" (
        call "%~dp0_say.bat" setup_ffmpeg_download
        call "%~dp0install_ffmpeg.bat"
    ) else (
        call "%~dp0_say.bat" setup_ffmpeg_bundled
    )
) else (
    call "%~dp0_say.bat" already_installed
)
REM SoX: a fallback in the capture chain (soundcard - ffmpeg - SoX) and the
REM voice-activated recorder (required). Kept if present; otherwise winget,
REM then the official SourceForge zip (SHA-256 checked).
"%PY%" -m syncnote.sox_fetch --dest "%REPO%\tools"
if errorlevel 1 (
    call "%~dp0_say.bat" setup_sox_failed
)

echo.
call "%~dp0_say.bat" setup_5
set "TS_EXE="
where tailscale >nul 2>&1 && set "TS_EXE=tailscale"
if not defined TS_EXE if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS_EXE=%ProgramFiles%\Tailscale\tailscale.exe"
if defined TS_EXE (
    call "%~dp0_say.bat" already_installed
) else (
    where winget >nul 2>&1
    if errorlevel 1 (
        call "%~dp0_say.bat" setup_ts_no_winget
    ) else (
        call "%~dp0_say.bat" setup_ts_winget
        winget install --id Tailscale.Tailscale -e --silent --accept-package-agreements --accept-source-agreements
        if errorlevel 1 (
            call "%~dp0_say.bat" setup_ts_failed
        ) else (
            call "%~dp0_say.bat" done
        )
    )
)

echo.
call "%~dp0_say.bat" setup_6
if not exist "%REPO%\config.toml" (
    copy "%REPO%\config.example.toml" "%REPO%\config.toml" >nul
    call "%~dp0_say.bat" setup_config_made
) else (
    call "%~dp0_say.bat" setup_config_kept
)
REM Turn the microphone permission on if it is off (the machine-wide switch
REM only with administrator rights).
"%PY%" -m syncnote devices --allow-mic

echo.
call "%~dp0_say.bat" setup_next
pause
