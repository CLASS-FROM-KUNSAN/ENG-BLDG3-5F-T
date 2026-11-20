@echo off
REM =====================================================================
REM  소리만 녹음합니다 (터미널 녹음기: FFmpeg / SoX).
REM  설정은 config.toml 의 [recording.terminal] 을 따릅니다.
REM
REM    record_audio.bat                    설정대로 한 번
REM    record_audio.bat -t 3000 -f mp3     50분, mp3
REM    record_audio.bat --vad --loop       소리가 날 때마다 파일 하나씩 (SoX)
REM
REM  작업 스케줄러용이라 끝나도 pause 하지 않습니다. 정해진 시각에
REM  창 없이 돌리려면 register_tasks.ps1 -AudioAt 09:00 을 쓰세요.
REM  파일 이름에 날짜와 시각이 들어가므로 매일 돌려도 덮어쓰지 않습니다.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m syncnote audio record %*
exit /b %ERRORLEVEL%
