"""릴리스 업로더.

여기 있는 테스트 대부분은 '조용한 데이터 손실' 을 막기 위한 것입니다.
녹화본이 백업됐다고 믿었는데 0바이트였다는 상황이 가장 나쁩니다.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from gymhall.release import (
    GITHUB_ASSET_LIMIT,
    ReleaseError,
    ReleaseUploader,
    asset_name,
    split_file,
    tag_for,
)


# ---------------------------------------------------------------------------
# 가짜 GitHub
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, body: dict | list | None = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body) if body else "error"
        self.content = b"x"
        self.headers: dict[str, str] = {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class FakeSession:
    """요청을 기록하고, 미리 정한 응답을 돌려줍니다."""

    def __init__(self, script=None):
        self.headers: dict[str, str] = {}
        self.calls: list[dict] = []
        self.script = script or []

    def request(self, method, url, **kw):
        data = kw.get("data")
        body = data.read() if hasattr(data, "read") else (data or b"")
        self.calls.append({"method": method, "url": url, "sent": len(body), "kw": kw})
        if self.script:
            handler = self.script.pop(0)
            return handler(len(body))
        # 에셋 목록 조회는 배열을 돌려줍니다(실제 GitHub 와 동일).
        if method == "GET" and "/assets" in url:
            return FakeResponse(200, [])
        if method == "DELETE":
            return FakeResponse(204)
        return FakeResponse(200, {"name": "x", "size": len(body)})


def make_uploader(script=None, **kw) -> tuple[ReleaseUploader, FakeSession]:
    up = ReleaseUploader("owner", "repo", token="t", retry_backoff=0.001, **kw)
    session = FakeSession(script)
    up._session = session
    return up, session


def fake_release(up: ReleaseUploader, tag: str = "tag") -> dict:
    release = {"id": 1, "tag_name": tag, "upload_url": "https://u/assets{?name}"}
    up._release_cache[tag] = release
    up._asset_cache[1] = []
    return release


# ---------------------------------------------------------------------------
# 에셋 이름
# ---------------------------------------------------------------------------
def test_asset_name_keeps_ascii_untouched():
    assert asset_name("lecture_01.mp4") == "lecture_01.mp4"
    assert asset_name("20261111_0900_part001.mkv") == "20261111_0900_part001.mkv"


def test_korean_names_get_unique_hash():
    """GitHub 는 비ASCII 를 버립니다. 해시 없이는 서로 다른 파일이 충돌합니다."""
    a = asset_name("운동과건강 1주차.pptx")
    b = asset_name("생명과학의이해 1주차.pptx")
    assert a != b, "서로 다른 과목 자료가 같은 이름이 됐습니다"
    assert a.endswith(".pptx") and b.endswith(".pptx")
    assert all(ord(c) < 128 for c in a)


def test_asset_name_is_stable():
    """같은 입력은 항상 같은 이름 — 중복 업로드 판단이 흔들리면 안 됩니다."""
    assert asset_name("과제 1.hwp") == asset_name("과제 1.hwp")


def test_asset_name_prefix_and_length():
    assert asset_name("a.bin", prefix="pc1").startswith("pc1_")
    assert len(asset_name("x" * 400 + ".mkv")) <= 200


# ---------------------------------------------------------------------------
# 태그
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "strategy,expected",
    [
        ("monthly", "rec-2026-11"),
        ("weekly", "rec-2026-W46"),
        ("daily", "rec-2026-11-11"),
        ("single", "rec-archive"),
    ],
)
def test_tag_for(strategy, expected):
    assert tag_for(strategy, "rec", date(2026, 11, 11), fixed="archive") == expected


# ---------------------------------------------------------------------------
# 분할
# ---------------------------------------------------------------------------
def test_split_restore_roundtrip_is_byte_exact(tmp_path):
    """쪼갠 조각을 순서대로 붙이면 원본과 바이트 단위로 같아야 합니다."""
    data = os.urandom(5_000_000)
    src = tmp_path / "big.mkv"
    src.write_bytes(data)

    parts = split_file(src, tmp_path / "p", 1_500_000)
    assert len(parts) == 4
    joined = b"".join(p.path.read_bytes() for p in sorted(parts, key=lambda x: x.index))
    assert joined == data


def test_split_exact_multiple(tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"A" * 3000)
    parts = split_file(src, tmp_path / "p", 1000)
    assert len(parts) == 3
    assert all(p.path.stat().st_size == 1000 for p in parts)


def test_chunk_size_never_exceeds_github_limit():
    up, _ = make_uploader(split_size_mb=99999)
    assert up.chunk_bytes < GITHUB_ASSET_LIMIT


# ---------------------------------------------------------------------------
# 업로드: 조용한 데이터 손실 방지
# ---------------------------------------------------------------------------
def test_retry_resends_the_whole_file(tmp_path):
    """회귀 방지: 파일 핸들을 한 번만 넘기면 재시도가 0바이트를 보냅니다.

    GitHub 는 빈 에셋도 받아들이고 200 을 돌려주므로, 사용자는 녹화본이
    백업된 줄 알지만 실제로는 0바이트입니다.
    """
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 5000)

    script = [
        lambda n: FakeResponse(500),                        # 첫 시도 실패
        lambda n: FakeResponse(200, {"name": "a.bin", "size": n}),
    ]
    up, session = make_uploader(script, retry_count=2)
    up.upload_one(fake_release(up), src, name="a.bin")

    sent = [c["sent"] for c in session.calls]
    assert sent == [5000, 5000], f"재시도가 전체를 다시 보내지 않았습니다: {sent}"


def test_network_error_retry_resends_whole_file(tmp_path):
    import requests

    src = tmp_path / "a.bin"
    src.write_bytes(b"Y" * 4096)

    class Flaky(FakeSession):
        def request(self, method, url, **kw):
            data = kw.get("data")
            body = data.read() if hasattr(data, "read") else (data or b"")
            self.calls.append({"method": method, "url": url, "sent": len(body), "kw": kw})
            if len(self.calls) == 1:
                raise requests.ConnectionError("끊김")
            return FakeResponse(200, {"name": "a.bin", "size": len(body)})

    up = ReleaseUploader("o", "r", token="t", retry_count=2, retry_backoff=0.001)
    session = Flaky()
    up._session = session
    up.upload_one(fake_release(up), src, name="a.bin")
    assert [c["sent"] for c in session.calls] == [4096, 4096]


def test_size_mismatch_is_an_error(tmp_path):
    """GitHub 가 보고한 크기가 다르면 성공으로 넘기지 않습니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 5000)
    up, _ = make_uploader([lambda n: FakeResponse(200, {"name": "a.bin", "size": 0})])
    with pytest.raises(ReleaseError, match="크기가 다릅니다"):
        up.upload_one(fake_release(up), src, name="a.bin")


def test_asset_list_is_fetched_once(tmp_path):
    """파일마다 에셋 목록을 조회하면 API 호출이 두 배가 됩니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 10)
    up, session = make_uploader()
    release = fake_release(up)
    del up._asset_cache[1]  # 캐시를 비워 실제 조회가 일어나게 합니다

    for i in range(10):
        up.upload_one(release, src, name=f"f{i}.bin")

    gets = [c for c in session.calls if c["method"] == "GET" and "/assets" in c["url"]]
    assert len(gets) == 1, f"에셋 목록을 {len(gets)}번 조회했습니다"


def test_existing_asset_is_replaced(tmp_path):
    """같은 이름이 있으면 지우고 다시 올립니다(GitHub 는 덮어쓰기를 막습니다)."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 10)
    up, session = make_uploader()
    release = fake_release(up)
    up._asset_cache[1] = [{"id": 77, "name": "a.bin"}]

    up.upload_one(release, src, name="a.bin")
    deletes = [c for c in session.calls if c["method"] == "DELETE"]
    assert len(deletes) == 1 and "77" in deletes[0]["url"]


def test_401_is_not_retried(tmp_path):
    """토큰이 틀렸으면 재시도해도 같습니다. 바로 알려 줘야 합니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X")
    up, session = make_uploader([lambda n: FakeResponse(401)], retry_count=4)
    with pytest.raises(ReleaseError, match="401"):
        up.upload_one(fake_release(up), src, name="a.bin")
    assert len(session.calls) == 1


# ---------------------------------------------------------------------------
# dry-run
# ---------------------------------------------------------------------------
def test_dry_run_makes_no_network_calls(tmp_path):
    """토큰이 없어도, 저장소가 없어도 무엇이 올라갈지 볼 수 있어야 합니다."""
    src = tmp_path / "a.bin"
    src.write_bytes(b"X" * 100)

    class Exploding:
        headers: dict[str, str] = {}

        def request(self, *a, **kw):
            raise AssertionError("dry-run 이 네트워크를 호출했습니다")

    up = ReleaseUploader("o", "r", token="t", dry_run=True)
    up._session = Exploding()
    result = up.upload(src, "recordings-2026-11")
    assert result["split"] is False


def test_dry_run_splits_and_cleans_up(tmp_path):
    src = tmp_path / "big.mkv"
    src.write_bytes(os.urandom(5_000_000))
    up = ReleaseUploader("o", "r", token="t", split_size_mb=1, dry_run=True)
    result = up.upload(src, "tag", prefix="pc1")
    assert result["split"] is True
    assert len(result["assets"]) == 6          # 조각 5개 + manifest
    assert not list(tmp_path.rglob("*.part*")), "임시 조각이 남았습니다"


def test_upload_missing_file_raises(tmp_path):
    up = ReleaseUploader("o", "r", token="t", dry_run=True)
    with pytest.raises(ReleaseError, match="없습니다"):
        up.upload(tmp_path / "ghost.mkv", "tag")


def test_owner_repo_required():
    with pytest.raises(ReleaseError):
        ReleaseUploader("", "repo")
