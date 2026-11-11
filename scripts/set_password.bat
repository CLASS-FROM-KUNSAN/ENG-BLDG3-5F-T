@echo off
REM =====================================================================
REM  비밀번호와 토큰을 Windows 자격 증명 관리자에 저장합니다.
REM  입력한 값은 화면에 보이지 않고, 파일에도 평문으로 남지 않습니다.
REM =====================================================================
call "%~dp0_common.bat"

echo ======================================================
echo  자격증명 저장
echo ======================================================
echo.
echo  [1] Eclass 비밀번호
echo  [2] GitHub 토큰
echo  [3] OpenAI API 키 (클라우드 전사를 쓸 때만)
echo  [4] 현재 상태 보기
echo  [0] 끝내기
echo.
set /p CHOICE="번호를 고르세요: "

if "%CHOICE%"=="1" "%PY%" -m gymhall secrets set eclass
if "%CHOICE%"=="2" "%PY%" -m gymhall secrets set github
if "%CHOICE%"=="3" "%PY%" -m gymhall secrets set openai
if "%CHOICE%"=="4" "%PY%" -m gymhall secrets status
echo.
pause
