@echo off
REM 녹화/자료가 저장된 폴더를 엽니다.
call "%~dp0_common.bat"
for /f "usebackq delims=" %%P in (`"%PY%" -c "import sys;sys.path.insert(0,r%REPO%\src);from gymhall import config;print(config.load().data_dir)"`) do set "DATA=%%P"
if exist "%DATA%" (start "" explorer "%DATA%") else (echo 폴더가 아직 없습니다: %DATA% & pause)
