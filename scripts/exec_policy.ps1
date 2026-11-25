<#
.SYNOPSIS
    PowerShell 실행 정책을 확인하고, .ps1 을 실행할 수 없는 정책이면 영구적으로 켭니다.

.DESCRIPTION
    이 프로젝트의 모든 .bat 이 _common.bat 에서 이 스크립트를 먼저 실행하고(정규 과정),
    작업 스케줄러 자동 등록(src/syncnote/tasks.py)도 등록 전에 실행합니다.

      1. Get-ExecutionPolicy -List 로 영구 정책을 확인합니다.
         (이 스크립트 자신은 -ExecutionPolicy Bypass 로 실행되므로 Process 범위는 빼고 봅니다 —
          그대로 Get-ExecutionPolicy 를 부르면 늘 Bypass 로 나옵니다.)
      2. 실행할 수 없는 정책(Restricted·AllSigned·Undefined)이면
         Set-ExecutionPolicy RemoteSigned -Scope CurrentUser 로 영구 설정합니다(레지스트리에
         저장되어 재부팅해도 유지, 관리자 권한 필요 없음). 관리자 권한으로 실행 중이면
         LocalMachine(PC 전체)도 같이 켭니다.
      3. 인터넷에서 받은 표시(MOTW, Zone.Identifier)가 붙은 이 폴더의 .ps1 은 차단을 풉니다
         (RemoteSigned 는 받은 파일 중 서명 없는 것을 막습니다).

    RemoteSigned 를 씁니다(Unrestricted 아님): 이 PC 에서 만든 스크립트는 실행하고,
    인터넷에서 받은 것은 서명이 있어야 실행합니다.

    그룹 정책(MachinePolicy·UserPolicy)으로 막혀 있으면 바꿀 수 없습니다 — 그때는 알리기만
    합니다. 이 프로젝트가 .ps1 을 부를 때는 늘 -ExecutionPolicy Bypass 를 주므로 우리 작업은
    그래도 돕니다(막히는 것은 .ps1 을 직접 더블클릭·실행할 때).

    무슨 일이 있어도 종료 코드 0 — 정책 문제로 녹화·스크립트를 멈추지 않습니다.

.PARAMETER Show
    정상이어도 현재 정책을 한 줄 보여 줍니다(setup.bat·doctor.bat).

.PARAMETER CheckOnly
    확인만 하고 바꾸지 않습니다(doctor 의 점검 항목).

.PARAMETER Json
    결과를 JSON 한 줄로(src/syncnote/exec_policy.py 가 읽음). 다른 출력은 하지 않습니다.
#>
param(
    [switch]$Show,
    [switch]$CheckOnly,
    [switch]$Json
)

$ErrorActionPreference = "Continue"
$Good = @("RemoteSigned", "Unrestricted", "Bypass")

function Get-Persistent {
    # Process 범위를 뺀 우선순위: MachinePolicy > UserPolicy > CurrentUser > LocalMachine.
    # 모두 Undefined 면 Windows(클라이언트) 기본값 Restricted.
    $list = Get-ExecutionPolicy -List
    foreach ($scope in @("MachinePolicy", "UserPolicy", "CurrentUser", "LocalMachine")) {
        $p = ($list | Where-Object { "$($_.Scope)" -eq $scope }).ExecutionPolicy
        if ("$p" -and "$p" -ne "Undefined") { return @{ Policy = "$p"; Scope = $scope } }
    }
    return @{ Policy = "Restricted"; Scope = "Default" }
}

function Get-ScopePolicy([string]$Scope) {
    "$((Get-ExecutionPolicy -List | Where-Object { "$($_.Scope)" -eq $Scope }).ExecutionPolicy)"
}

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

$result = [ordered]@{
    before = ""; before_scope = ""; after = ""; after_scope = ""
    changed = @(); gpo_blocked = $false; unblocked = @(); error = ""
}

try {
    $b = Get-Persistent
    $result.before = $b.Policy
    $result.before_scope = $b.Scope
    $result.gpo_blocked = ($b.Scope -in @("MachinePolicy", "UserPolicy")) -and ($Good -notcontains $b.Policy)

    if (-not $CheckOnly) {
        if (($Good -notcontains $b.Policy) -and -not $result.gpo_blocked) {
            # Set-ExecutionPolicy 는 Process 범위(Bypass)가 덮어쓴다는 '오류' 를 내도 값은 저장합니다.
            # 그래서 성공 여부는 다시 읽어서 판단합니다.
            try { Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force -ErrorAction Stop } catch { }
            if ((Get-ScopePolicy "CurrentUser") -eq "RemoteSigned") {
                $result.changed += "CurrentUser"
            } else {
                $result.error = "현재 사용자 범위에 RemoteSigned 를 저장하지 못했습니다"
            }
        }
        if ((Test-Admin) -and ($Good -notcontains (Get-ScopePolicy "LocalMachine")) -and -not $result.gpo_blocked) {
            try { Set-ExecutionPolicy RemoteSigned -Scope LocalMachine -Force -ErrorAction Stop } catch { }
            if ((Get-ScopePolicy "LocalMachine") -eq "RemoteSigned") { $result.changed += "LocalMachine" }
        }
        # 인터넷에서 받은 표시(MOTW)가 붙은 .ps1 차단 해제
        foreach ($f in Get-ChildItem -Path $PSScriptRoot -Filter *.ps1 -File -ErrorAction SilentlyContinue) {
            if (Get-Item -LiteralPath $f.FullName -Stream Zone.Identifier -ErrorAction SilentlyContinue) {
                try { Unblock-File -LiteralPath $f.FullName -ErrorAction Stop; $result.unblocked += $f.Name } catch { }
            }
        }
    }

    $a = Get-Persistent
    $result.after = $a.Policy
    $result.after_scope = $a.Scope
} catch {
    $result.error = "$($_.Exception.Message)"
}

if ($Json) {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $result | ConvertTo-Json -Compress
    exit 0
}

$ok = $Good -contains $result.after
if ($result.changed.Count -gt 0) {
    Write-Host ("PowerShell 실행 정책: {0} -> RemoteSigned 로 영구 설정했습니다 ({1})." -f
        $result.before, ($result.changed -join ", ")) -ForegroundColor Green
} elseif ($Show -and $ok) {
    Write-Host ("PowerShell 실행 정책: {0} ({1}) - 스크립트를 실행할 수 있습니다." -f
        $result.after, $result.after_scope)
}
if ($result.unblocked.Count -gt 0) {
    Write-Host ("인터넷에서 받은 표시로 막혀 있던 스크립트의 차단을 풀었습니다: {0}" -f
        ($result.unblocked -join ", ")) -ForegroundColor Green
}
if (-not $ok) {
    if ($result.gpo_blocked) {
        Write-Host ("PowerShell 실행 정책이 그룹 정책({0}: {1})으로 막혀 있어 바꿀 수 없습니다." -f
            $result.after_scope, $result.after) -ForegroundColor Yellow
        Write-Host "  이 프로젝트의 작업은 -ExecutionPolicy Bypass 로 실행하므로 그대로 돕니다."
        Write-Host "  .ps1 을 직접 실행하려면 PC 관리자에게 정책 변경을 요청하세요."
    } elseif ($CheckOnly) {
        Write-Host ("PowerShell 실행 정책: {0} - .ps1 을 실행할 수 없습니다(다음 스크립트 실행 때 켭니다)." -f
            $result.after) -ForegroundColor Yellow
    } else {
        Write-Host ("PowerShell 실행 정책을 켜지 못했습니다({0}). {1}" -f $result.after, $result.error) -ForegroundColor Yellow
        Write-Host "  직접 켜기: PowerShell 에서  Set-ExecutionPolicy RemoteSigned -Scope CurrentUser"
    }
}
exit 0
