@echo off
REM Open the folder where recordings and materials are stored.
call "%~dp0_common.bat"
REM _common.bat already sets PYTHONPATH. Touching sys.path here again would expand
REM r%REPO%\src into rC:\repo\src, a SyntaxError (a raw prefix r needs a quote).
REM "call" first: cmd strips the outer quotes of a for /f command that starts with a
REM quote and has more quotes, breaking a PY path with spaces (C:\Program Files\...).
for /f "usebackq delims=" %%P in (`call "%PY%" -c "from syncnote import config;print(config.load().data_dir)"`) do set "DATA=%%P"
if not defined DATA (
    call "%~dp0_say.bat" odf_no_config
    pause
    exit /b 1
)
if exist "%DATA%" (start "" explorer "%DATA%") else (
    call "%~dp0_say.bat" odf_missing
    echo %DATA%
    pause
)
