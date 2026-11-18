@echo off
REM =====================================================================
REM  네이티브 메시징 호스트 런처 — 브라우저 확장이 이걸 띄웁니다.
REM  stdout 은 프레임 전용이라 아무것도 출력하면 안 됩니다(그래서 _common.bat
REM  을 call 하지 않고 최소한만 설정합니다).
REM  등록은 extension\tools\register_host.ps1 이 합니다.
REM =====================================================================
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
if exist "%REPO%\config.toml" set "GYMHALL_CONFIG=%REPO%\config.toml"
if exist "%REPO%\.venv\Scripts\python.exe" (
    set "PY=%REPO%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
"%PY%" -m gymhall.activity.native_host
