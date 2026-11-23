@echo off
REM 파이썬을 어떤 방법으로도 실행하지 못했을 때 PY 가 가리키는 안내. _common.bat 이 정합니다.
REM "%PY%" -m ... 로 불리므로 받은 인자는 무시하고 안내만 한 뒤 실패로 끝냅니다.
echo.
echo [X] 파이썬을 실행하지 못했습니다. 다음을 차례로 시도했습니다:
echo       SYNCNOTE_PYTHON, python, py -3, python3, py
echo     해결 방법 중 하나:
echo       1^) https://www.python.org/downloads/ 에서 3.11 이상을 설치하세요.
echo          설치할 때 "Add python.exe to PATH" 를 체크하세요. py 런처도 함께 설치됩니다.
echo       2^) 설정 - 앱 - 고급 앱 설정 - 앱 실행 별칭 에서 python.exe / python3.exe 를 끄세요.
echo          Microsoft Store 의 가짜 python 이 진짜 파이썬을 가립니다.
echo       3^) 이미 설치한 파이썬이 있으면 그 경로를 환경 변수 SYNCNOTE_PYTHON 에 적으세요.
echo          예: setx SYNCNOTE_PYTHON "C:\Python312\python.exe"
echo.
exit /b 9009
