<#
.SYNOPSIS
    작업 스케줄러에 Sync Note 작업을 등록합니다.

.DESCRIPTION
    보통은 직접 실행할 필요가 없습니다. setup.bat·doctor.bat·시간표 편집기, 그리고 모든
    syncnote 명령이 시작할 때 작업이 빠졌거나 다른 폴더(옛 레포)를 가리키면 이 스크립트를
    자동으로 실행합니다(syncnote 의 tasks ensure 명령, src/syncnote/tasks.py).

      powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1

    관리자 권한은 보통 필요 없습니다(자기 계정의 '로그온한 경우에만 실행' 작업). 권한이
    없어 거부되면 종료 코드 5 로 끝나고, 사람이 실행한 setup/doctor 는 UAC 로 다시 시도합니다.

    등록되는 작업 (작업 스케줄러의 Sync Note 폴더 아래):
      EclassSync      - 지정한 간격마다 Eclass 자료 수집
      RecordSchedule  - 로그온 시 시작해 시간표대로 녹화
      RecordWatch     - 로그온 시 시작해 지정 프로그램 감지
      AutoUpdate      - 로그온 10분 뒤부터 한 시간마다 git pull origin main(녹화 중이면 건너뜀,
                        config.toml 의 [update])
      RecordGuard     - 로그온 시 시작해(15분마다 다시 확인) 녹화 중이 아닐 때 녹화 파일을
                        확인하고 못 올린 것을 올림 (config.toml 의 [recording.guard])
      TranscribeQueue - 밤마다 밀린 전사 처리
      HomeBackup      - 매일 홈 폴더를 GitHub 릴리스에 백업 (config.toml 의 backup.home.enabled)
      HomePrune       - 매일 백업이 확인된 홈 파일 정리 (config.toml 의 backup.home.prune.enabled)
      TerminalAudio   - (선택) -AudioAt 을 주면 매일 그 시각에 소리만 녹음
      StreamSchedule  - (선택) -Stream 을 주면 로그온 시 시작해 시간표대로 실시간 송출

    중요:
      화면 캡처는 로그온된 세션이 있어야 합니다. 그래서 모든 작업을
      "사용자가 로그온한 경우에만 실행"(Interactive)으로 만듭니다.
      이걸 "로그온 여부에 관계없이 실행"으로 바꾸면 녹화 결과가
      검은 화면만 남습니다.

.PARAMETER User
    작업을 실행할 계정(기본: 지금 계정). UAC 로 다른 관리자 계정이 승격해 실행할 때도
    작업은 원래 사용자의 로그온 세션에서 돌아야 하므로 tasks.py 가 원래 이름을 넘깁니다.

.PARAMETER Yes
    묻지 않고 등록합니다. 없으면, 새로 등록하거나 바꿀 작업이 있을 때 목록을 보여 주고
    '등록할까요? (Y/N)' 를 묻습니다(이미 맞게 등록돼 있으면 묻지 않음). syncnote 의
    tasks ensure 는 자기가 먼저 물어보고(뒤에서 하는 자동 등록은 묻지 않음) -Yes 로 부릅니다.
    물을 수 없는 창(입력 없음)에서 -Yes 가 없으면 등록하지 않고 종료 코드 2.

.PARAMETER Folder
    작업 스케줄러 폴더(기본 "\Sync Note"). 시험할 때 다른 폴더에 등록했다 지우려고 둡니다.
    tasks.py 는 기본 폴더만 봅니다.

.PARAMETER Remove
    등록된 작업을 모두 지웁니다.

.PARAMETER SkipHome
    HomeBackup / HomePrune 작업을 등록하지 않습니다.
    등록해도 config.toml 에서 켜지 않으면 아무것도 하지 않습니다.

.PARAMETER AudioAt
    "09:00" 처럼 주면 매일 그 시각에 record_audio.bat(FFmpeg/SoX 터미널
    녹음기)을 창 없이 돌립니다. 길이와 장치는 config.toml 의
    [recording.terminal] 을 따르고, -AudioArgs 로 덮어쓸 수 있습니다.
      -AudioAt 09:00 -AudioArgs "-t","3000","-f","mp3"

.PARAMETER Stream
    로그온 시 stream_schedule.bat 을 창 없이 띄워, config.toml 의 시간표대로
    수업마다 Tailscale 실시간 송출을 하고 링크를 이슈로 올립니다([stream]).
#>
param(
    [switch]$Remove,
    [int]$EclassIntervalHours = 3,
    [string]$TranscribeTime = "02:40",
    [string]$HomeBackupTime = "03:13",
    [string]$HomePruneTime = "05:43",
    [switch]$SkipHome,
    [switch]$SkipWatch,
    [switch]$SkipSchedule,
    [string]$AudioAt = "",
    [string[]]$AudioArgs = @(),
    [switch]$Stream,
    [string]$User = $env:USERNAME,
    [string]$Folder = "\Sync Note",
    [int]$UpdateIntervalMinutes = 60,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

# 실행 정책 확인·영구 설정(정규 과정, scripts\exec_policy.ps1 — 모든 스크립트가 먼저 거침).
# 이미 실행 중인 이 창은 영향받지 않고, 다음에 .ps1 을 직접 실행할 때 막히지 않게 합니다.
& (Join-Path $PSScriptRoot "exec_policy.ps1")
$Repo    = Split-Path -Parent $PSScriptRoot
$Scripts = Join-Path $Repo "scripts"
$Vbs     = Join-Path $Scripts "hidden.vbs"

# 권한이 없어 거부되면 종료 코드 5(tasks.py 가 보고 UAC 로 다시 시도). 그 밖의 실패는 1.
# 주의: ScheduledTasks 명령(Get/Register/Unregister-ScheduledTask 등)은 모듈 함수라, 이 스크립트를
# 프롬프트에서 .\register_tasks.ps1 로 실행하면 위의 $ErrorActionPreference = "Stop" 을 보지
# 못합니다 — 오류가 나도 계속 진행해 '등록:' 을 찍고 '완료' 로 끝났습니다(강의실 PC 에서 실제로).
# 그래서 이 명령들에는 모두 -ErrorAction 을 직접 줍니다.
trap {
    $msg = "$($_.Exception.Message)"
    if ($_.Exception -is [UnauthorizedAccessException] -or $_.Exception.HResult -eq -2147024891 -or
        $msg -match "Access is denied|액세스가 거부|0x80070005") {
        Write-Host "작업 등록이 거부됐습니다(권한 부족): $msg" -ForegroundColor Red
        Write-Host "  이미 있는 작업이 관리자 권한 창(또는 SSH)에서 만들어졌으면, 일반 창에서는 같은"
        Write-Host "  계정이라도 지우거나 바꿀 수 없습니다. 관리자 권한 PowerShell 에서 다시 실행하거나"
        Write-Host "  scripts\setup.bat / scripts\doctor.bat 을 실행해 UAC 창에서 '예' 를 누르세요."
        Write-Host "  (이미 맞게 등록된 작업은 바꾸지 않으므로, 바꿀 것이 없으면 이 오류는 나지 않습니다.)"
        exit 5
    }
    Write-Host "작업 등록 실패: $msg" -ForegroundColor Red
    exit 1
}

function Remove-All {
    foreach ($name in @("EclassSync","RecordSchedule","RecordWatch","RecordGuard","TranscribeQueue","HomeBackup","HomePrune","TerminalAudio","StreamSchedule","AutoUpdate")) {
        if (Get-ScheduledTask -TaskPath "$Folder\" -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskPath "$Folder\" -TaskName $name -Confirm:$false -ErrorAction Stop
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
    New-ScheduledTaskAction -Execute "wscript.exe" -Argument $argLine -WorkingDirectory $Repo -ErrorAction Stop
}

function Register-One([string]$Name, $Action, $Trigger, [string]$Description) {
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

    $principal = New-ScheduledTaskPrincipal -UserId $User `
        -LogonType Interactive -RunLevel Limited

    # 이미 맞게 등록돼 있으면 그대로 둡니다(지웠다 다시 만들지 않음). 관리자 창에서 만든 작업은
    # 일반 창에서 지울 수 없어서, 바꿀 것이 없는데도 '액세스 거부' 로 실패했습니다.
    $existing = Get-ScheduledTask -TaskPath "$Folder\" -TaskName $Name -ErrorAction SilentlyContinue
    $same = $existing -and (Test-SameTask $existing $Action $Trigger $Description)
    if ($script:Planning) {
        # 1단계: 무엇이 바뀌는지만 모음(사람에게 물어보려고). 아무것도 바꾸지 않음.
        if (-not $existing) { $script:Changes += "새로 등록: $Folder\$Name" }
        elseif (-not $same) { $script:Changes += "바꿔 다시 등록: $Folder\$Name (설정이 다름)" }
        elseif ("$($existing.State)" -eq "Disabled") { $script:Changes += "켬: $Folder\$Name (꺼져 있음)" }
        return
    }
    if ($same) {
        if ("$($existing.State)" -eq "Disabled") {
            Enable-ScheduledTask -TaskPath "$Folder\" -TaskName $Name -ErrorAction Stop | Out-Null
            Write-Host "  켬:   $Folder\$Name (꺼져 있었음)" -ForegroundColor Green
        } else {
            Write-Host "  그대로: $Folder\$Name (이미 맞게 등록됨)"
        }
        return
    }
    if ($existing) {
        Unregister-ScheduledTask -TaskPath "$Folder\" -TaskName $Name -Confirm:$false -ErrorAction Stop
    }
    Register-ScheduledTask -TaskPath $Folder -TaskName $Name -Action $Action `
        -Trigger $Trigger -Settings $settings -Principal $principal `
        -Description $Description -ErrorAction Stop | Out-Null
    Write-Host "  등록: $Folder\$Name" -ForegroundColor Green
}

function Test-SameTask($Existing, $Action, $Trigger, [string]$Description) {
    # 실행할 것(프로그램·인자·폴더), 계정(이름 끝부분 — '1121' 과 'PC\1121'), 로그온 방식,
    # 설명(주기·시각이 들어 있음), 트리거 종류가 같으면 같은 작업으로 봅니다.
    $a = @($Existing.Actions)[0]
    if (-not $a) { return $false }
    if ("$($a.Execute)" -ne "$($Action.Execute)") { return $false }
    if ("$($a.Arguments)" -ne "$($Action.Arguments)") { return $false }
    if ("$($a.WorkingDirectory)" -ne "$($Action.WorkingDirectory)") { return $false }
    $u = ("$($Existing.Principal.UserId)" -split "\\")[-1]
    if ($u -ne (("$User" -split "\\")[-1])) { return $false }
    if ("$($Existing.Principal.LogonType)" -ne "Interactive") { return $false }
    if ("$($Existing.Principal.RunLevel)" -ne "Limited") { return $false }
    if ("$($Existing.Description)" -ne $Description) { return $false }
    $had = @($Existing.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ","
    $want = @($Trigger | ForEach-Object { $_.CimClass.CimClassName }) -join ","
    return ($had -eq $want)
}

if ($Remove) { Remove-All; exit 0 }

if (-not (Test-Path (Join-Path $Repo "config.toml"))) {
    Write-Host "config.toml 이 없습니다. 먼저 scripts\setup.bat 을 실행하세요." -ForegroundColor Red
    exit 1
}

Write-Host "작업 스케줄러 등록을 확인합니다..." -ForegroundColor Cyan
Write-Host "  레포: $Repo"
Write-Host "  계정: $User"

$RegisterAll = {
    # --- Eclass 주기 수집 ---
    # 정각을 피해 17분에 시작합니다. 정각에는 다른 작업이 몰립니다.
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(17) `
            -RepetitionInterval (New-TimeSpan -Hours $EclassIntervalHours)
    Register-One "EclassSync" (New-HiddenAction "eclass_sync.bat" @()) $t `
        "Eclass 자료를 ${EclassIntervalHours}시간마다 수집해 GitHub 릴리스에 올립니다."

    # --- 시간표 녹화 ---
    if (-not $SkipSchedule) {
        $t = New-ScheduledTaskTrigger -AtLogOn -User $User -ErrorAction Stop
        $t.Delay = "PT1M"
        Register-One "RecordSchedule" (New-HiddenAction "record_schedule.bat" @()) $t `
            "로그온 후 대기하다가 config.toml 의 시간표대로 녹화합니다."
    }

    # --- 프로그램 감지 녹화 ---
    if (-not $SkipWatch) {
        $t = New-ScheduledTaskTrigger -AtLogOn -User $User -ErrorAction Stop
        $t.Delay = "PT2M"
        Register-One "RecordWatch" (New-HiddenAction "record_watch.bat" @()) $t `
            "Zoom 등 지정한 프로그램이 실행되면 자동으로 녹화합니다."
    }

    # --- 녹화 지킴이 ---
    # 로그온 때 띄우고, 15분마다 다시 띄웁니다(이미 돌고 있으면 IgnoreNew 로 새로 안 띄움).
    # 꺼졌다 켜진 뒤·작업이 12시간 제한으로 끝난 뒤에도 다시 살아나게 하려는 것입니다.
    $t = New-ScheduledTaskTrigger -AtLogOn -User $User -ErrorAction Stop
    $t.Delay = "PT4M"
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes 15)).Repetition
    Register-One "RecordGuard" (New-HiddenAction "record_guard.bat" @()) $t `
        "녹화 중이 아닐 때 녹화 파일을 확인하고(꺼진 녹화 합치기·망가진 파일 다시 만들기) 못 올린 것을 올립니다."

    # --- 레포 자동 업데이트 ---
    # 로그온 10분 뒤부터 한 시간마다 git pull origin main(녹화 중이면 건너뜀, src/syncnote/updater.py).
    $t = New-ScheduledTaskTrigger -AtLogOn -User $User -ErrorAction Stop
    $t.Delay = "PT10M"
    $t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $UpdateIntervalMinutes)).Repetition
    Register-One "AutoUpdate" (New-HiddenAction "update.bat" @()) $t `
        "레포를 ${UpdateIntervalMinutes}분마다 git pull origin main 으로 받습니다(녹화 중이면 건너뜀)."

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

    # --- 터미널 녹음 (선택) ---
    if ($AudioAt) {
        $t = New-ScheduledTaskTrigger -Daily -At $AudioAt
        Register-One "TerminalAudio" (New-HiddenAction "record_audio.bat" $AudioArgs) $t `
            "매일 $AudioAt 에 소리만 녹음합니다(FFmpeg/SoX, [recording.terminal])."
    }

    # --- 시간표 실시간 송출 (선택) ---
    if ($Stream) {
        $t = New-ScheduledTaskTrigger -AtLogOn -User $User -ErrorAction Stop
        $t.Delay = "PT3M"
        Register-One "StreamSchedule" (New-HiddenAction "stream_schedule.bat" @()) $t `
            "로그온 후 대기하다가 시간표대로 수업마다 실시간 송출합니다([stream])."
    }
}

# 1단계: 무엇이 바뀌는지 모으기 → 있으면 사람에게 묻기 → 2단계: 실제로 등록
$script:Planning = $true
$script:Changes = @()
& $RegisterAll
$script:Planning = $false
if ($script:Changes.Count -eq 0) {
    Write-Host "  모든 작업이 이미 맞게 등록돼 있습니다. 바꿀 것이 없습니다." -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "작업 스케줄러에 다음을 합니다:" -ForegroundColor Yellow
foreach ($c in $script:Changes) { Write-Host "  - $c" }
if (-not $Yes) {
    $answer = $null
    try { $answer = Read-Host "등록할까요? (Y/N)" } catch { $answer = $null }
    if ($null -eq $answer) {
        Write-Host "물어볼 수 없는 창이라 등록하지 않았습니다(-Yes 로 실행하면 묻지 않고 등록)." -ForegroundColor Yellow
        exit 2
    }
    if ("$answer".Trim() -notmatch "^(y|yes|예|ㅛ)$") {
        Write-Host "등록하지 않았습니다. 다음에 setup.bat·doctor.bat 을 실행하면 다시 묻습니다." -ForegroundColor Yellow
        exit 2
    }
}
Write-Host ""
& $RegisterAll

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
