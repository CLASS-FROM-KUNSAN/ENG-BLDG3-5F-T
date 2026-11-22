"""Playwright 브라우저 받기 — 학교망 인증서 검사(self-signed certificate)."""

import ssl
import types

from syncnote import playwright_setup as P
from syncnote import setup_failures

CERT_OUT = ("Downloading Chrome Headless Shell 156.0.8078.4 ...\nError: self-signed certificate in "
            "certificate chain\n  code: 'SELF_SIGNED_CERT_IN_CHAIN'\nFailed to install browsers\n")


def _runs(*results):
    calls = []

    def run(cmd, env):
        calls.append((cmd, dict(env)))
        rc, out = results[len(calls) - 1]
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
    return run, calls


def test_cert_error_retries_once_with_windows_store(tmp_path):
    pem = tmp_path / "ca.pem"
    run, calls = _runs((1, CERT_OUT), (0, "downloaded"))
    rc = P.install(run=run, ca=lambda: (pem, 90), say=lambda _m: None)
    assert rc == 0 and len(calls) == 2
    assert "NODE_EXTRA_CA_CERTS" not in calls[0][1]
    assert calls[1][1]["NODE_EXTRA_CA_CERTS"] == str(pem)
    assert calls[1][0][-3:] == ["playwright", "install", "chromium"]
    assert all("NODE_TLS_REJECT_UNAUTHORIZED" not in env for _c, env in calls)   # 검증을 끄지 않음


def test_cert_error_even_with_exit_zero_counts():
    # playwright 는 일부만 실패해도 0 으로 끝날 때가 있어 출력으로 판정
    run, calls = _runs((0, CERT_OUT), (0, "ok"))
    assert P.install(run=run, ca=lambda: (None, 0), say=lambda _m: None) == 1
    assert len(calls) == 1                                       # 저장소가 없으면 재시도 못 함


def test_other_failures_do_not_retry_and_success_is_quiet():
    run, calls = _runs((1, "Error: ENOSPC: no space left"))
    assert P.install(run=run, ca=lambda: (None, 0), say=lambda _m: None) == 1 and len(calls) == 1
    run, calls = _runs((0, "chromium downloaded"))
    assert P.install(run=run, ca=lambda: (None, 0), say=lambda _m: None) == 0 and len(calls) == 1


def test_still_failing_tells_what_to_do():
    run, _calls = _runs((1, CERT_OUT), (1, CERT_OUT))
    said = []
    assert P.install(run=run, ca=lambda p=None: ("x.pem", 3), say=said.append) == 1
    assert any("루트 인증 기관" in m for m in said) and any("테더링" in m for m in said)


def test_windows_store_export_dedups_and_skips_non_x509(tmp_path):
    der = ssl.PEM_cert_to_DER_cert(ssl.DER_cert_to_PEM_cert(b"\x30\x03\x02\x01\x01"))
    stores = {"ROOT": [(der, "x509_asn", True), (b"crl", "pkcs_7_asn", True)],
              "CA": [(der, "x509_asn", True)]}
    path, n = P.windows_ca_pem(tmp_path / "w.pem", enum=lambda s: stores[s])
    assert n == 1 and path.read_text(encoding="ascii").count("BEGIN CERTIFICATE") == 1
    assert P.windows_ca_pem(tmp_path / "x.pem", enum=lambda s: []) == (None, 0)


def test_failure_classifier_knows_the_node_message():
    cause = setup_failures.classify(CERT_OUT, 1)
    assert cause is not None and cause.id == "ssl"
