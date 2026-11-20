"""ingest 파이프라인 + 네이티브 호스트 메시지 처리.

게이트/암호화 없이(FakeStore) 정책→봉인 호출 계약만 검증합니다. 게이트는
저장소 open 단계에서 강제되므로 여기서는 돌지 않습니다.
"""

from __future__ import annotations

from syncnote.activity import native_host, pipeline
from syncnote.activity.policy import FF_PASSWORD, Policy


class FakeStore:
    def __init__(self):
        self.sealed: list[dict] = []

    def seal(self, event):
        self.sealed.append(event)


def policy() -> Policy:
    return Policy(origins=("https://acrobat.adobe.com",), name_blacklist=("*ssn*",))


def msg(**over):
    base = {
        "v": 1, "adapter": "pdfjs",
        "scope": {"origin": "https://acrobat.adobe.com", "doc_hash": "d1", "field_path": "FullName"},
        "field": {"name": "FullName", "type": "text", "flags": 0},
        "value": "Kim", "commit_reason": "change", "ts": 10,
    }
    base.update(over)
    return base


def test_clean_field_is_sealed_with_redacted_value():
    store = FakeStore()
    out = pipeline.ingest(store, policy(), msg(value="주민 900101-1234567"))
    assert out == "sealed" and len(store.sealed) == 1
    assert "[주민번호]" in store.sealed[0]["value"]
    assert store.sealed[0]["scope"]["doc_hash"] == "d1"


def test_password_field_is_dropped_not_sealed():
    store = FakeStore()
    out = pipeline.ingest(store, policy(), msg(field={"name": "pw", "flags": FF_PASSWORD}))
    assert out == "drop:password_field" and store.sealed == []


def test_off_allowlist_origin_is_dropped():
    store = FakeStore()
    out = pipeline.ingest(store, policy(), msg(scope={"origin": "https://evil.test", "doc_hash": "d"}))
    assert out == "drop:non_allowlisted" and store.sealed == []


def test_signature_type_is_dropped():
    store = FakeStore()
    out = pipeline.ingest(store, policy(), msg(field={"name": "sig", "type": "signature"}))
    assert out == "drop:signature_field"


def test_name_blacklist_dropped():
    store = FakeStore()
    out = pipeline.ingest(store, policy(), msg(field={"name": "applicant_ssn", "type": "text"}))
    assert out == "drop:name_blacklist"


# -- 네이티브 호스트 메시지 라우팅 -------------------------------------------
def test_handle_message_routes_field_commit_to_ingest():
    store = FakeStore()
    assert native_host.handle_message(msg(), store, policy()) == "sealed"
    assert len(store.sealed) == 1


def test_handle_message_ignores_non_field_shapes():
    store = FakeStore()
    assert native_host.handle_message({"probe": {"href": "x"}}, store, policy()) == "ignored"
    assert native_host.handle_message({"v": 1}, store, policy()) == "ignored"
    assert store.sealed == []
