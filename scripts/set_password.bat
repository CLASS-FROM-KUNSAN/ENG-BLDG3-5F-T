@echo off
REM =====================================================================
REM  비밀번호와 토큰을 Windows 자격 증명 관리자에 저장합니다.
REM  입력한 값은 화면에 보이지 않고, 파일에도 평문으로 남지 않습니다.
REM
REM  번호를 인자로 주면 메뉴 없이 바로 그 항목으로 갑니다.
REM    set_password.bat 1     Eclass 비밀번호
REM  (시간표 편집기가 eClass 과목 목록을 받다가 비밀번호가 없으면 이렇게 엽니다.)
REM =====================================================================
call "%~dp0_common.bat"

set "CHOICE=%~1"
if not "%CHOICE%"=="" goto :run

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

:run
if "%CHOICE%"=="1" "%PY%" -m syncnote secrets set eclass
if "%CHOICE%"=="2" "%PY%" -m syncnote secrets set github
if "%CHOICE%"=="3" "%PY%" -m syncnote secrets set openai
if "%CHOICE%"=="4" "%PY%" -m syncnote secrets status
echo.
if not "%~1"=="" echo  다 됐으면 아무 키나 눌러 이 창을 닫으세요. 편집기가 과목 목록을 다시 가져옵니다.
pause