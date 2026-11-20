@echo off
call "%~dp0_common.bat"
REM  시간표 편집기(GUI). 저장하면 config.toml 의 [[recording.timetable]] 이 바뀝니다.
"%PY%" -m syncnote timetable edit
if errorlevel 1 pause
