@echo off
REM =====================================================================
REM  Release helper: create and push a vX.Y.Z tag.
REM  The push triggers the GitHub Actions release workflow
REM  (changelog + source zip).
REM  Usage:  scripts\release.bat v0.3.0
REM =====================================================================
setlocal
call "%~dp0_common.bat"

set "TAG=%~1"
if "%TAG%"=="" (
    call "%~dp0_say.bat" rel_usage
    endlocal & exit /b 1
)

REM Only vX.Y.Z - a typo must not publish a wrong tag.
echo %TAG%|findstr /r "^v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*$" >nul
if errorlevel 1 (
    call "%~dp0_say.bat" rel_bad_tag
    echo %TAG%
    endlocal & exit /b 1
)

cd /d "%REPO%"

REM Stop if the tag exists - never overwrite an existing release.
git rev-parse -q --verify "refs/tags/%TAG%" >nul 2>&1
if not errorlevel 1 (
    call "%~dp0_say.bat" rel_exists
    echo %TAG%
    endlocal & exit /b 1
)

call "%~dp0_say.bat" rel_pushing
echo %TAG%
git tag "%TAG%"
if errorlevel 1 (
    call "%~dp0_say.bat" rel_tag_failed
    endlocal & exit /b 1
)
git push origin "%TAG%"
if errorlevel 1 (
    call "%~dp0_say.bat" rel_push_failed
    echo %TAG%
    endlocal & exit /b 1
)

echo.
call "%~dp0_say.bat" rel_done
echo   https://github.com/seongbin45/gym-w-hall-2f/actions
endlocal & exit /b 0
