<#
.SYNOPSIS
    syncnote 활동 기록 네이티브 메시징 호스트를 등록합니다(확장 -> 로컬 Python).

.DESCRIPTION
    관리자 권한이 필요 없습니다(HKCU). 확장을 chrome://extensions 에서 압축해제
    로드한 뒤, 거기 표시되는 확장 ID 를 -ExtensionId 로 넘기세요.

      powershell -ExecutionPolicy Bypass -File scripts\register_host.ps1 -ExtensionId <ID>

    하는 일:
      1) 호스트 매니페스트(com.syncnote.activity.json)를 extension\host\ 에 생성.
         path  = scripts\activity_host.bat (_find_python.bat 가 고른 %PY% 로 native_host 를 띄움)
         allowed_origins = [ chrome-extension://<ID>/ ]
      2) HKCU\...\NativeMessagingHosts\com.syncnote.activity = 그 매니페스트 경로.

.PARAMETER Remove
    등록을 지웁니다.
#>
param(
    [string]$ExtensionId,
    [ValidateSet("chrome", "edge", "both")][string]$Browser = "both",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$HostName = "com.syncnote.activity"
$Repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$Launcher = Join-Path $Repo "scripts\activity_host.bat"
$HostDir = Join-Path $Repo "extension\host"
$Manifest = Join-Path $HostDir "$HostName.json"

function Reg-Keys {
    $keys = @()
    if ($Browser -in @("chrome", "both")) { $keys += "HKCU:\Software\Google\Chrome\NativeMessagingHosts\$HostName" }
    if ($Browser -in @("edge", "both")) { $keys += "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\$HostName" }
    return $keys
}

if ($Remove) {
    foreach ($k in Reg-Keys) {
        if (Test-Path $k) { Remove-Item $k -Force; Write-Host "삭제: $k" }
    }
    if (Test-Path $Manifest) { Remove-Item $Manifest -Force; Write-Host "삭제: $Manifest" }
    Write-Host "등록을 지웠습니다." -ForegroundColor Green
    exit 0
}

if (-not $ExtensionId) {
    Write-Host "확장 ID 가 필요합니다. chrome://extensions 에서 이 확장을 로드한 뒤 표시되는 ID 를 주세요:" -ForegroundColor Red
    Write-Host "  register_host.ps1 -ExtensionId abcd...(32자)"
    exit 1
}
if ($ExtensionId -notmatch '^[a-p]{32}$') {
    Write-Host "확장 ID 형식이 이상합니다(소문자 a-p 32자여야 합니다): $ExtensionId" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Launcher)) { throw "런처가 없습니다: $Launcher" }

New-Item -ItemType Directory -Force $HostDir | Out-Null
$manifestObj = [ordered]@{
    name            = $HostName
    description     = "syncnote activity native messaging host"
    path            = $Launcher
    type            = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
}
# -Depth 로 배열이 펼쳐지게, UTF-8(BOM 없이) 저장.
$json = $manifestObj | ConvertTo-Json -Depth 5
[System.IO.File]::WriteAllText($Manifest, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Host "매니페스트: $Manifest" -ForegroundColor Green

foreach ($k in Reg-Keys) {
    New-Item -Path $k -Force | Out-Null
    Set-ItemProperty -Path $k -Name "(default)" -Value $Manifest
    Write-Host "레지스트리: $k" -ForegroundColor Green
}

Write-Host ""
Write-Host "완료. 브라우저를 재시작하면 확장이 네이티브 호스트에 연결됩니다." -ForegroundColor Green
Write-Host "확인: config.toml 에서 activity.enabled=true, machine_is_private=true,"
Write-Host "      activity.adapters.acrobat_web=true, activity.origins 에 뷰어 오리진."
