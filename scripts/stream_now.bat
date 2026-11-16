@echo off
REM =====================================================================
REM  실시간 오디오 송출을 시작합니다 (Tailscale 망 안에서만).
REM  링크가 생기면 GitHub 이슈로 올라갑니다. 설정은 config.toml 의 [stream].
REM
REM    stream_now.bat                     설정대로
REM    stream_now.bat -l 자료구조 -m 100  이름과 길이
REM    stream_now.bat --source mic        마이크로
REM    stream_now.bat --no-issue          이슈 없이
REM
REM  이 창에서 Ctrl+C 를 누르면 멈추고, tailscale serve 를 끄고, 이슈를 닫습니다.
REM =====================================================================
call "%~dp0_common.bat"
"%PY%" -m gymhall stream start %*
echo.
pause
