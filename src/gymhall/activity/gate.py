"""하드웨어/OS 무결성 게이트 — fail-closed.

봉인 저장은 이 게이트를 통과해야만 시작합니다. 하나라도 실패하면 저장 API 가
GateError 를 던지고, 메모리 버퍼는 폐기되며, 임시 평문 파일은 만들어지지 않습니다.

정직한 한계(숨기면 안 됨): 이 게이트는 **정직하게 약한 기기**(TPM 2.0 없음,
Secure Boot 실제로 꺼짐, 관리자 권한 실행, 디버거 부착)의 저장을 확실히 막습니다.
그러나 **이미 커널/루트를 쥔 공격자**는 아래 프로브 호출 자체를 속일 수 있습니다 —
이건 방어심화(defense-in-depth)이지 '깨끗한 OS 의 암호학적 증명'이 아닙니다.
완전한 TPM 원격증명(EK 인증서 체인 + quote 검증)은 아직 구현돼 있지 않아,
require_attestation 을 켜면 **거짓 통과 대신 fail-closed** 로 막습니다.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass

from ..logging_setup import get as get_logger

log = get_logger("activity.gate")


class GateError(Exception):
    """게이트 실패로 저장/복호를 거부. 메시지에 민감 데이터 없음."""


@dataclass
class GateResult:
    ok: bool
    failures: list[str]           # 이유 코드만
    details: dict[str, str]

    def summary(self) -> str:
        return "통과" if self.ok else "실패: " + ", ".join(self.failures)


def _ps(script: str, timeout: float = 20) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=flags,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:200])
    return (proc.stdout or "").strip()


# -- 프로브: 각각 (ok, detail). 예외는 호출부에서 실패로 처리 ------------------
def probe_tpm2() -> tuple[bool, str]:
    # 비관리자에서 읽히는 신호만 씁니다(Get-Tpm/Win32_Tpm 은 관리자 전용이라,
    # 비관리자 실행을 강제하는 이 게이트와 충돌합니다):
    #  - TPM 2.0 존재: ACPI\MSFT0101 (Microsoft 가 쓰는 TPM 2.0 ACPI HID)
    #  - 저장 준비: TPM\WMI 레지스트리의 TaskReadyForStorage
    out = _ps(
        "$dev = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | "
        "Where-Object { $_.DeviceID -match 'MSFT0101' }; "
        "$w = Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\TPM\\WMI' "
        "-ErrorAction SilentlyContinue; "
        '"$([bool]$dev)|$($w.TaskReadyForStorage)"'
    )
    present, ready = (out.split("|") + ["", ""])[:2]
    ok = present.strip().lower() == "true" and ready.strip() == "1"
    return ok, f"tpm2_present={present.strip()},ready_for_storage={ready.strip() or 'n/a'}"


def probe_secure_boot() -> tuple[bool, str]:
    # 레지스트리로 읽습니다(Confirm-SecureBootUEFI 는 관리자 전용). 레거시 BIOS 는
    # 키가 없어 n/a → 실패(Secure Boot 미지원이므로 올바른 fail-closed).
    out = _ps(
        "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\SecureBoot\\State' "
        "-Name UEFISecureBootEnabled -ErrorAction SilentlyContinue).UEFISecureBootEnabled"
    )
    return out.strip() == "1", f"uefi_secure_boot={out.strip() or 'n/a'}"


def probe_vbs_hvci() -> tuple[bool, str]:
    out = _ps(
        "$g=Get-CimInstance -Namespace root/Microsoft/Windows/DeviceGuard "
        "-ClassName Win32_DeviceGuard -ErrorAction Stop; "
        '"$($g.VirtualizationBasedSecurityStatus)|$($g.SecurityServicesRunning -join \',\')"'
    )
    vbs, running = (out.split("|") + ["", ""])[:2]
    running_set = {x.strip() for x in running.split(",")}
    ok = vbs.strip() == "2" and "2" in running_set   # VBS running + HVCI running
    return ok, f"vbs={vbs.strip()},hvci={'2' in running_set}"


def probe_not_elevated() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "not_windows"
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    return (not admin), f"elevated={admin}"


def probe_no_debugger() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "not_windows"
    present = bool(ctypes.windll.kernel32.IsDebuggerPresent())
    return (not present), f"debugger={present}"


# 이유코드 → (설정 플래그, 프로브). 설정에서 끄면 그 프로브는 건너뜁니다.
_PROBES: dict[str, tuple[str, object]] = {
    "tpm_version": ("require_tpm2", probe_tpm2),
    "secure_boot": ("require_secure_boot", probe_secure_boot),
    "vbs_hvci": ("require_vbs_hvci", probe_vbs_hvci),
    "elevated": ("require_not_elevated", probe_not_elevated),
    "debugger": ("require_no_debugger", probe_no_debugger),
}


def check_gate(gate_cfg: dict) -> GateResult:
    """설정이 요구하는 프로브를 모두 돌려 종합. 프로브 예외 = 그 항목 실패."""
    if os.name != "nt":
        # 활동 캡처는 Windows 전용. 다른 OS 는 안전 측에서 저장 거부.
        return GateResult(False, ["gate_fail:os_unsupported"], {"os": os.name})

    failures: list[str] = []
    details: dict[str, str] = {}
    for code, (flag, probe) in _PROBES.items():
        if not gate_cfg.get(flag, True):
            continue
        try:
            ok, detail = probe()  # type: ignore[operator]
        except Exception as exc:
            ok, detail = False, f"probe_error:{exc}"[:120]
        details[code] = detail
        if not ok:
            failures.append(f"gate_fail:{code}")

    # 완전한 원격증명은 미구현 — 켜면 fail-closed(거짓 통과 금지).
    if gate_cfg.get("require_attestation", False):
        failures.append("gate_fail:attestation_not_implemented")
        details["attestation"] = "not_implemented"

    ok = not failures
    log.info("게이트 %s", "통과" if ok else "실패 " + ",".join(failures))
    return GateResult(ok, failures, details)


def require_for_write(gate_cfg: dict) -> GateResult:
    """저장 경로 최상단. 실패 시 GateError — 평문은 어디에도 안 남깁니다."""
    res = check_gate(gate_cfg)
    if not res.ok:
        raise GateError(
            "하드웨어/OS 무결성 게이트 실패로 저장을 거부합니다 — "
            f"{', '.join(res.failures)}. 환경을 복구한 뒤 다시 검사하세요. "
            "민감 데이터는 저장되지 않았습니다."
        )
    return res


def require_for_read(gate_cfg: dict, *, break_glass: bool = False) -> GateResult:
    """읽기(복호) 경로.

    read_enforces_gate=True(기본)면 대칭 차단: 게이트 실패는 '전용 기기' 전제가
    깨졌다는 신호이므로, 그 위에서 복호해 평문을 오염된 OS 에 노출하지 않습니다.
    영구 잠김을 피하려 break_glass=True 로 한 번 명시적으로 열 수 있습니다(로그 남김).
    read_enforces_gate=False 면 경고만 하고 차단하지 않습니다.
    """
    res = check_gate(gate_cfg)
    if not gate_cfg.get("read_enforces_gate", True):
        if not res.ok:
            log.warning("게이트 실패 상태지만 read_enforces_gate=False 라 복호를 허용합니다: %s",
                        ", ".join(res.failures))
        return res
    if res.ok:
        return res
    if break_glass:
        log.warning("브레이크글라스로 게이트 실패 상태에서 복호를 허용합니다: %s",
                    ", ".join(res.failures))
        return res
    raise GateError(
        "게이트 실패 상태라 복호를 막습니다(read_enforces_gate=true). "
        f"{', '.join(res.failures)}. 정말 열려면 --break-glass 로 한 번만 허용하세요."
    )
