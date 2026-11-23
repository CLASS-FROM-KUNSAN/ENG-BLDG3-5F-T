@echo off
REM =====================================================================
REM  Install ffmpeg. setup.bat calls this when ffmpeg is missing.
REM  python -m syncnote.ffmpeg_fetch --auto tries, by cause, in order:
REM    0) an ffmpeg that already works (PATH, tools, winget, pip; features checked)
REM    1) ffmpeg*.zip placed in tools (offline, e.g. from USB)
REM    2) pip imageio-ffmpeg (bundled ffmpeg; no admin rights, no winget)
REM    3) winget install Gyan.FFmpeg (agreement prompts answered). If winget is
REM       missing or blocked by policy, remember it and skip next time
REM       (--retry-skipped to try again)
REM    4) the real release list (GyanD/codexffmpeg), newest first; on a hash or
REM       feature-check failure roll back to the previous release; stop at once
REM       when the disk is full
REM    5) ffmpeg*.zip in the Downloads folder
REM    If all fail it prints the order tried, the reasons and one next step.
REM  Without any Python: winget -> gyan.dev zip downloaded with PowerShell.
REM  winget changes PATH, but not in this window, so the result is checked by
REM  the file winget created (syncnote looks there too; no new window needed).
REM =====================================================================
call "%~dp0_common.bat"
set "URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "ZIP=%TEMP%\ffmpeg-syncnote.zip"
set "DEST=%REPO%\tools"
set "WG_LINK=%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"

"%PY%" -m syncnote.ffmpeg_fetch --auto --dest "%DEST%"
if not errorlevel 1 exit /b 0
REM 9009 = no Python -> the ways that need no Python (winget, PowerShell zip)
if errorlevel 9009 goto :nopython
REM The order tried, reasons and next step were printed above.
pause & exit /b 1

:nopython
call :winget_found
if not errorlevel 1 (
    call "%~dp0_say.bat" ff_winget_already
    echo %WG_FOUND%
    exit /b 0
)

:winget

where winget >nul 2>&1
if errorlevel 1 (
    call "%~dp0_say.bat" ff_no_winget
    goto :release
)
call "%~dp0_say.bat" ff_winget_install
REM --accept-*-agreements   : answer Y to every 'Do you agree ...?' prompt
REM --source winget         : no regional-agreement prompt from the msstore source
REM --disable-interactivity : if any prompt is left, end instead of waiting
call winget install --id Gyan.FFmpeg -e --source winget --silent --disable-interactivity --accept-source-agreements --accept-package-agreements
call :winget_found
if not errorlevel 1 (
    call "%~dp0_say.bat" ff_done_at
    echo %WG_FOUND%
    call "%~dp0_say.bat" ff_new_window
    exit /b 0
)
call "%~dp0_say.bat" ff_winget_unfinished

:release
"%PY%" -m syncnote.ffmpeg_fetch --dest "%DEST%"
if not errorlevel 1 exit /b 0
REM 9009 = no Python found -> download without Python
if errorlevel 9009 goto :zip
goto :fail

:zip
call "%~dp0_say.bat" ff_ps_download
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 goto :fail

call "%~dp0_say.bat" ff_unzip
if not exist "%DEST%" mkdir "%DEST%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%ZIP%' -DestinationPath '%DEST%\_ffmpeg_tmp' -Force"

for /d %%D in ("%DEST%\_ffmpeg_tmp\ffmpeg-*") do (
    if exist "%DEST%\ffmpeg" rmdir /s /q "%DEST%\ffmpeg"
    move "%%D" "%DEST%\ffmpeg" >nul
)
rmdir /s /q "%DEST%\_ffmpeg_tmp" 2>nul
del "%ZIP%" 2>nul

if exist "%DEST%\ffmpeg\bin\ffmpeg.exe" (
    call "%~dp0_say.bat" ff_done_at
    echo %DEST%\ffmpeg\bin\ffmpeg.exe
) else (
    call "%~dp0_say.bat" ff_no_exe
    exit /b 1
)
exit /b 0

:fail
echo.
call "%~dp0_say.bat" ff_fail
echo        %DEST%\ffmpeg\bin\ffmpeg.exe
call "%~dp0_say.bat" ff_fail_winget
pause & exit /b 1

REM If winget created ffmpeg.exe, put it in WG_FOUND and return errorlevel 0
:winget_found
set "WG_FOUND="
if exist "%WG_LINK%" set "WG_FOUND=%WG_LINK%"
if not defined WG_FOUND for /d %%P in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*") do (
    for /d %%B in ("%%P\ffmpeg-*") do if exist "%%B\bin\ffmpeg.exe" set "WG_FOUND=%%B\bin\ffmpeg.exe"
)
if defined WG_FOUND exit /b 0
exit /b 1
