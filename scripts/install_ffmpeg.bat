@echo off
REM =====================================================================
REM  ffmpeg 를 tools\ffmpeg 에 내려받습니다.
REM =====================================================================
call "%~dp0_common.bat"
set "URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "ZIP=%TEMP%\ffmpeg-gymhall.zip"
set "DEST=%REPO%\tools"

echo ffmpeg 를 내려받습니다... (약 80MB)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%URL%' -OutFile '%ZIP%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo.
    echo [X] 내려받기 실패. 직접 설치하세요:
    echo     1) %URL% 에서 zip 을 받습니다
    echo     2) 압축을 풀어 bin\ffmpeg.exe 가
    echo        %DEST%\ffmpeg\bin\ffmpeg.exe 가 되도록 둡니다
    pause & exit /b 1
)

echo 압축을 푸는 중...
if not exist "%DEST%" mkdir "%DEST%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Expand-Archive -Path '%ZIP%' -DestinationPath '%DEST%\_ffmpeg_tmp' -Force"

for /d %%D in ("%DEST%\_ffmpeg_tmp\ffmpeg-*") do (
    if exist "%DEST%\ffmpeg" rmdir /s /q "%DEST%\ffmpeg"
    move "%%D" "%DEST%\ffmpeg" >nul
)
rmdir /s /q "%DEST%\_ffmpeg_tmp" 2>nul
del "%ZIP%" 2>nul

if exist "%DEST%\ffmpeg\bin\ffmpeg.exe" (
    echo 완료: %DEST%\ffmpeg\bin\ffmpeg.exe
) else (
    echo [X] 압축 해제 후 ffmpeg.exe 를 찾지 못했습니다.
    exit /b 1
)
