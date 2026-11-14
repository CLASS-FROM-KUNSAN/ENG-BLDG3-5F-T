<#
.SYNOPSIS
    작업 스케줄러에 GYM-W-HALL-2F 작업을 등록합니다.

.DESCRIPTION
    관리자 권한 PowerShell 에서 실행하세요.

      powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1

    등록되는 작업 (작업 스케줄러의 GYM-W-HALL-2F 폴더 아래):
      EclassSync      - 지정한 간격마다 Eclass 자료 수집
      RecordSchedule  - 로그온 시 시작해 시간표대로 녹화
      RecordWatch     - 로그온 시 시작해 지정 프로그램 감지
      TranscribeQueue - 밤마다 밀린 전사 처리
      HomeBackup      - 매일 홈 폴더를 GitHub 릴리스에 백업 (config.toml 의 backup.home.enabled)
      HomePrune       - 매일 백업이 확인된 홈 파일 정리 (config.toml 의 backup.home.prune.enabled)

    중요:
      화면 캡처는 로그온된 세션이 있어야 합니다. 그래서 모든 작업을
      "사용자가 로그온한 경우에만 실행"(Interactive)으로 만듭니다.
      이걸 "로그온 여부에 관계없이 실행"으로 바꾸면 녹화 결과가
      검은 화면만 남습니다.

.PARAMETER Remove
    등록된 작업을 모두 지웁니다.

.PARAMETER SkipHome
    HomeBackup / HomePrune 작업을 등록하지 않습니다.
    등록해도 config.toml 에서 켜지 않으면 아무것도 하지 않습니다.
#>
param(
    [switch]$Remove,
    [int]$EclassIntervalHours = 3,
    [string]$TranscribeTime = "02:40",
    [string]$HomeBackupTime = "03:13",
    [string]$HomePruneTime = "05:43",
    [switch]$SkipHome,
    [switch]$SkipWatch,
    [switch]$SkipSchedule
)

$ErrorActionPreference = "Stop"
$Folder  = "\GYM-W-HALL-2F"
$Repo    = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path $Repo "scripts"
$Vbs     = Join-Path $Scripts "hidden.vbs"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "관리자 권한이 필요합니다." -ForegroundColor Red
        Write-Host "PowerShell 을 오른쪽 클릭 -> 관리자 권한으로 실행 후 다시 시도하세요."
        exit 1
    }
}

function Remove-All {
    foreach ($name in @("EclassSync","RecordSchedule","RecordWatch","TranscribeQueue","HomeBackup","HomePrune")) {
        if (Get-ScheduledTask -TaskPath "$Folder\" -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskPath "$Folder\" -TaskName $name -Confirm:$false
            Write-Host "  삭제: $Folder\$name"
        }
    }
    Write-Host "작업을 모두 삭제했습니다." -ForegroundColor Green
}

function New-HiddenAction([string]$BatName, [string[]]$ExtraArgs) {
    $bat = Join-Path $Scripts $BatName
    if (-not (Test-Path $bat)) { throw "없는 스크립트: $bat" }
    $argLine = "`"$Vbs`" `"$bat`""
    foreach ($a in $ExtraArgs) { $argLine += " `"$a`"" }
    New-ScheduledTaskAction -Execute "wscript.exe" -Argument $argLine -WorkingDirectory $Repo
}

function Register-One([string]$Name, $Action, $Trigger, [string]$Description) {
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited

    if (Get-ScheduledTask -TaskPath "$Folder\" -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskPath "$Folder\" -TaskName $Name -Confirm:$false
    }
    Register-ScheduledTask -TaskPath $Folder -TaskName $Name -Action $Action `
        -Trigger $Trigger -Settings $settings -Principal $principal `
        -Description $Description | Out-Null
    Write-Host "  등록: $Folder\$Name" -ForegroundColor Green
}

Assert-Admin

if ($Remove) { Remove-All; exit 0 }

if (-not (Test-Path (Join-Path $Repo "config.toml"))) {
    Write-Host "config.toml 이 없습니다. 먼저 scripts\setup.bat 을 실행하세요." -ForegroundColor Red
    exit 1
}

Write-Host "작업을 등록합니다..." -ForegroundColor Cyan
Write-Host "  레포: $Repo"

# --- Eclass 주기 수집 ---
# 정각을 피해 17분에 시작합니다. 정각에는 다른 작업이 몰립니다.
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(17) `
        -RepetitionInterval (New-TimeSpan -Hours $EclassIntervalHours)
Register-One "EclassSync" (New-HiddenAction "eclass_sync.bat" @()) $t `
    "Eclass 자료를 ${EclassIntervalHours}시간마다 수집해 GitHub 릴리스에 올립니다."

# --- 시간표 녹화 ---
if (-not $SkipSchedule) {
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $t.Delay = "PT1M"
    Register-One "RecordSchedule" (New-HiddenAction "record_schedule.bat" @()) $t `
        "로그온 후 대기하다가 config.toml 의 시간표대로 녹화합니다."
}

# --- 프로그램 감지 녹화 ---
if (-not $SkipWatch) {
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $t.Delay = "PT2M"
    Register-One "RecordWatch" (New-HiddenAction "record_watch.bat" @()) $t `
        "Zoom 등 지정한 프로그램이 실행되면 자동으로 녹화합니다."
}

# --- 야간 전사 ---
$t = New-ScheduledTaskTrigger -Daily -At $TranscribeTime
Register-One "TranscribeQueue" (New-HiddenAction "transcribe_run.bat" @()) $t `
    "밀린 음성 전사를 매일 $TranscribeTime 에 처리합니다."

# --- 홈 백업 / 정리 ---
# 정리는 백업보다 늦게 돕니다. 지우는 것은 GitHub 에 올라간 사슬이
# 확인된 파일뿐이고, 둘 다 config.toml 에서 켜야 실제로 일합니다.
if (-not $SkipHome) {
    $t = New-ScheduledTaskTrigger -Daily -At $HomeBackupTime
    Register-One "HomeBackup" (New-HiddenAction "home_backup.bat" @()) $t `
        "홈 폴더를 매일 $HomeBackupTime 에 GitHub 릴리스로 백업합니다(backup.home.enabled)."

    $t = New-ScheduledTaskTrigger -Daily -At $HomePruneTime
    Register-One "HomePrune" (New-HiddenAction "home_prune.bat" @()) $t `
        "백업이 확인된 홈 파일을 매일 $HomePruneTime 에 정리합니다(backup.home.prune.enabled)."
}

Write-Host ""
Write-Host "완료. 작업 스케줄러에서 '$Folder' 폴더를 확인하세요." -ForegroundColor Green
Write-Host ""
Write-Host "알아 두세요:" -ForegroundColor Yellow
Write-Host "  - 창이 안 뜨므로 오류는 로그 파일로만 보입니다."
Write-Host "    로그 위치는 scripts\doctor.bat 이 알려 줍니다."
Write-Host "  - 노트북을 들고 다니면 Eclass 수집은 다음에 켤 때 밀려 실행됩니다"
Write-Host "    (StartWhenAvailable). 반면 시간표 녹화는 수업을 통째로 놓칠 수"
Write-Host "    있으니, 중요한 수업은 record_now.bat 으로 직접 시작하세요."
Write-Host "  - 절전/최대 절전에서 깨어난 직후에는 오디오 장치 인식이 몇 초"
Write-Host "    늦습니다. pre_roll_minutes 를 2분 이상으로 두세요."
Write-Host ""
Write-Host "지우려면:  powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -Remove"
