"""하드웨어/OS 무결성 게이트 — fail-closed.

실제 TPM/Secure Boot 프로브는 실기에서만 돕니다. 여기서는 프로브를 바꿔 끼워
'하나라도 실패하면 저장 거부', '읽기 대칭 차단/브레이크글라스', '미구현 원격증명은
fail-closed' 를 검사합니다. os.name 을 'nt' 로 둬 리눅스 CI 에서도 로직을 탑니다.
"""

from __future__ import annotations

import pytest

from gymhall.activity import gate
from gymhall.activity.gate import GateError

ALL_PASS = {
    "tpm_version": ("require_tpm2", lambda: (True, "spec=2.0")),
    "secure_boot": ("require_secure_boot", lambda: (True, "secure_boot=True")),
    "vbs_hvci": ("require_vbs_hvci", lambda: (True, "vbs=2")),
    "elevated": ("require_not_elevated", lambda: (True, "elevated=False")),
    "debugger": ("require_no_debugger", lambda: (True, "debugger=False")),
}

GATE_ON = {
    "require_tpm2": True, "require_secure_boot": True, "require_vbs_hvci": True,
    "require_not_elevated": True, "require_no_debugger": True,
    "require_attestation": False, "read_enforces_gate": True,
}


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(gate.os, "name", "nt")


def _probes(monkeypatch, mapping):
    monkeypatch.setattr(gate, "_PROBES", mapping)


def test_all_pass_is_ok(monkeypatch, on_windows):
    _probes(monkeypatch, ALL_PASS)
    res = gate.check_gate(GATE_ON)
    assert res.ok and res.failures == []


def test_one_failing_probe_fails_the_gate(monkeypatch, on_windows):
    bad = dict(ALL_PASS, secure_boot=("require_secure_boot", lambda: (False, "secure_boot=False")))
    _probes(monkeypatch, bad)
    res = gate.check_gate(GATE_ON)
    assert not res.ok and "gate_fail:secure_boot" in res.failures
    assert "gate_fail:tpm_version" not in res.failures   # 나머지는 통과


def test_probe_exception_is_treated_as_failure(monkeypatch, on_windows):
    def boom():
        raise RuntimeError("TBS 접근 불가")

    _probes(monkeypatch, dict(ALL_PASS, tpm_version=("require_tpm2", boom)))
    res = gate.check_gate(GATE_ON)
    assert not res.ok and "gate_fail:tpm_version" in res.failures
    assert res.details["tpm_version"].startswith("probe_error:")


def test_disabled_check_is_skipped(monkeypatch, on_windows):
    # require_vbs_hvci=False 면 그 프로브는 돌지 않습니다(없는 기기에서 끌 수 있게).
    _probes(monkeypatch, dict(ALL_PASS, vbs_hvci=("require_vbs_hvci", lambda: (False, "x"))))
    cfg = dict(GATE_ON, require_vbs_hvci=False)
    res = gate.check_gate(cfg)
    assert res.ok and "vbs_hvci" not in res.details


def test_attestation_requested_but_unimplemented_fails_closed(monkeypatch, on_windows):
    _probes(monkeypatch, ALL_PASS)
    res = gate.check_gate(dict(GATE_ON, require_attestation=True))
    assert not res.ok and "gate_fail:attestation_not_implemented" in res.failures


def test_non_windows_is_unsupported(monkeypatch):
    monkeypatch.setattr(gate.os, "name", "posix")
    res = gate.check_gate(GATE_ON)
    assert not res.ok and res.failures == ["gate_fail:os_unsupported"]


# -- 쓰기/읽기 경로 -----------------------------------------------------------
def test_require_for_write_raises_on_failure(monkeypatch, on_windows):
    _probes(monkeypatch, dict(ALL_PASS, debugger=("require_no_debugger", lambda: (False, "debugger=True"))))
    with pytest.raises(GateError, match="저장을 거부"):
        gate.require_for_write(GATE_ON)


def test_require_for_write_returns_on_pass(monkeypatch, on_windows):
    _probes(monkeypatch, ALL_PASS)
    assert gate.require_for_write(GATE_ON).ok


def test_read_symmetric_blocks_on_failure(monkeypatch, on_windows):
    _probes(monkeypatch, dict(ALL_PASS, secure_boot=("require_secure_boot", lambda: (False, "off"))))
    with pytest.raises(GateError, match="복호를 막습니다"):
        gate.require_for_read(GATE_ON)


def test_read_break_glass_allows_despite_failure(monkeypatch, on_windows):
    _probes(monkeypatch, dict(ALL_PASS, secure_boot=("require_secure_boot", lambda: (False, "off"))))
    res = gate.require_for_read(GATE_ON, break_glass=True)
    assert not res.ok   # 통과가 아니라, '실패지만 명시적으로 허용'


def test_read_warn_only_when_not_enforced(monkeypatch, on_windows):
    _probes(monkeypatch, dict(ALL_PASS, secure_boot=("require_secure_boot", lambda: (False, "off"))))
    res = gate.require_for_read(dict(GATE_ON, read_enforces_gate=False))
    assert not res.ok   # 차단하지 않고 결과만 돌려줌(경고는 로그로)
