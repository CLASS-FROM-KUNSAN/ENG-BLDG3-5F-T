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


# ---------------------------------------------------------------------------
# 업로드 필터
#
# eclass.upload.allow_extensions 와 max_file_mb 는 설정 파일에 적혀
# 있고 CLI 가 읽어서 넘기기까지 했지만, 받는 쪽이 결과를 버려서
# **아무 효과가 없었습니다.** 화이트리스트를 적어도 전부 올라갔고,
# 용량 제한을 걸어도 2GB 파일이 그대로 올라갔습니다.
# ---------------------------------------------------------------------------
def _make(tmp_path, name: str, size: int = 10):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_upload_filter_passes_everything_by_default(tmp_path):
    from gymhall.release import upload_filter

    files = [_make(tmp_path, "a.pdf"), _make(tmp_path, "b.zip")]
    keep, dropped = upload_filter(files)
    assert keep == files
    assert dropped == []


def test_allow_extensions_actually_drops_other_files(tmp_path):
    from gymhall.release import upload_filter

    pdf = _make(tmp_path, "강의안.pdf")
    exe = _make(tmp_path, "setup.exe")
    keep, dropped = upload_filter([pdf, exe], allow_extensions=["pdf", ".PPTX"])
    assert keep == [pdf]
    assert [p for p, _ in dropped] == [exe]
    assert "exe" in dropped[0][1]


def test_max_file_mb_actually_drops_big_files(tmp_path):
    from gymhall.release import upload_filter

    small = _make(tmp_path, "small.pdf", 1024)
    big = _make(tmp_path, "big.pdf", 3 * 1024 * 1024)
    keep, dropped = upload_filter([small, big], max_file_mb=2)
    assert keep == [small]
    assert [p for p, _ in dropped] == [big]
    assert "제한 2MB" in dropped[0][1]


def test_dropped_files_stay_on_disk(tmp_path):
    """뺀 파일은 지우지 않습니다. 로컬 보관이 목적입니다."""
    from gymhall.release import upload_filter

    big = _make(tmp_path, "big.pdf", 2 * 1024 * 1024)
    upload_filter([big], max_file_mb=1)
    assert big.exists()


def test_missing_file_is_dropped_not_raised(tmp_path):
    from gymhall.release import upload_filter

    gone = tmp_path / "없는파일.pdf"
    keep, dropped = upload_filter([gone], max_file_mb=1)
    assert keep == []
    assert len(dropped) == 1


def test_extension_matching_ignores_case_and_dots(tmp_path):
    from gymhall.release import upload_filter

    f = _make(tmp_path, "자료.PDF")
    keep, _ = upload_filter([f], allow_extensions=[".pdf"])
    assert keep == [f]


# ---------------------------------------------------------------------------
# 에셋 이름 충돌
#
# 2026-11-12 실제 GitHub Actions 실행 기록:
#   업로드 시작: 809b1351.md (880 B)      <- 어느 과목의 강의목록.md
#   업로드 완료: 809b1351.md
#   같은 이름의 에셋을 교체합니다: 809b1351.md
#   업로드 시작: 809b1351.md (4.3 KiB)    <- 다른 과목의 강의목록.md
#   ...
# 9개 과목 × 2종 = 16개를 올렸는데 이름이 두 개뿐이라 서로 덮어썼고,
# 릴리스에는 2개만 남았습니다. 로그에는 '업로드 완료' 가 16번 찍혔고
# 작업은 성공으로 끝났습니다.
# ---------------------------------------------------------------------------
REAL_COURSES = [
    "기초웹프로그래밍_05", "생명과학의이해_01", "스키_01", "승마입문_01",
    "영어2_23", "운동과건강_01", "프로그래밍언어및실습_04",
    "[이수필수] 2026년 재학생 법정의무교육(성폭력·가정폭력)",
    "학생생활관 안내 및 안전교육(2)",
]


def _term_tree(tmp_path):
    """실제 실행과 같은 모양의 학기 폴더를 만듭니다."""
    root = tmp_path / "2026-2"
    made = []
    for i, course in enumerate(REAL_COURSES):
        for menu, stem in (("강의목록", "강의목록"), ("강의계획서", "강의계획서")):
            d = root / course / menu
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"{stem}.md"
            f.write_text(f"# {course} {menu}\n" + "x" * (i + 1), encoding="utf-8")
            made.append(f)
    return root, made


def test_same_filename_in_different_courses_got_one_asset_name(tmp_path):
    """고치기 전 동작을 못으로 박아 둡니다 — 파일 이름만 보면 전부 같습니다."""
    from gymhall.release import asset_name

    names = {asset_name("강의목록.md") for _ in REAL_COURSES}
    assert names == {"809b1351.md"}, names


def test_relative_path_makes_every_asset_name_unique(tmp_path):
    from gymhall.release import asset_name

    root, files = _term_tree(tmp_path)
    names = [
        asset_name(f.name, unique_key=str(f.relative_to(root)))
        for f in files
    ]
    assert len(names) == 18, len(names)
    assert len(set(names)) == len(names), "에셋 이름이 겹칩니다"


def test_upload_many_uploads_every_file_under_a_distinct_name(tmp_path):
    """실제 실행을 그대로 재현합니다. 하나도 덮어쓰지 않아야 합니다."""
    from gymhall.release import upload_many

    root, files = _term_tree(tmp_path)
    seen: list[str] = []

    class Spy:
        def upload(self, path, tag, *, prefix="", rel_path=None, **kw):
            import pathlib

            from gymhall.release import asset_name
            name = asset_name(pathlib.Path(path).name, prefix=prefix,
                              unique_key=rel_path or pathlib.Path(path).name)
            seen.append(name)
            return {"tag": tag, "assets": [{"name": name}], "split": False, "sha256": "x"}

    results = upload_many(Spy(), files, "tag", root=root)
    assert len(results) == len(files)
    assert len(set(seen)) == len(seen), f"덮어쓴 이름이 있습니다: {len(seen) - len(set(seen))}건"


def test_upload_many_refuses_to_overwrite_within_one_run(tmp_path, caplog):
    """이름이 겹치면 두 번째를 올리지 않고 기록을 남깁니다.

    root 를 주지 않으면 예전과 같은 상황이 됩니다. 그때도 조용히
    덮어쓰는 대신 멈춰야 합니다.
    """
    import logging

    from gymhall.release import upload_many

    _root, files = _term_tree(tmp_path)

    class Spy:
        def __init__(self):
            self.n = 0

        def upload(self, path, tag, *, prefix="", rel_path=None, **kw):
            self.n += 1
            return {"tag": tag, "assets": [{"name": "x"}], "split": False, "sha256": "x"}

    spy = Spy()
    with caplog.at_level(logging.ERROR):
        upload_many(spy, files, "tag")          # root 없음 = 예전 동작
    assert spy.n == 2, f"겹치는 이름을 {spy.n}번 올렸습니다"
    assert "겹칩니다" in caplog.text


def test_label_carries_the_relative_path(tmp_path):
    """에셋 이름이 해시라서, 어느 과목 파일인지는 label 이 유일한 단서입니다."""
    up, _ = make_uploader()
    rel = "2026-2/영어2_23/강의목록/강의목록.md"
    f = tmp_path / "강의목록.md"
    f.write_text("x", encoding="utf-8")
    seen = {}

    def fake_upload_one(release, path, *, name=None, label=None):
        seen.update(name=name, label=label)
        return {"name": name}

    up.upload_one = fake_upload_one
    up.get_or_create_release = lambda tag: {"id": 1, "upload_url": "u"}
    up.upload(f, "tag", rel_path=rel)
    assert seen["label"] == rel
    assert seen["name"].endswith(".md") and seen["name"] != "강의목록.md"


def test_upload_derives_the_asset_name_from_the_relative_path(tmp_path):
    """upload() 가 rel_path 를 실제로 이름에 쓰는지.

    label 만 보면 이름은 여전히 파일명 해시일 수 있습니다. 그러면
    과목이 달라도 같은 이름이 나와 서로 덮어씁니다.
    """
    up, _ = make_uploader()
    f = tmp_path / "강의목록.md"
    f.write_text("x", encoding="utf-8")
    names = []

    up.upload_one = lambda release, path, *, name=None, label=None: (
        names.append(name) or {"name": name}
    )
    up.get_or_create_release = lambda tag: {"id": 1, "upload_url": "u"}
    for course in ("영어2_23", "운동과건강_01", "스키_01"):
        up.upload(f, "tag", rel_path=f"2026-2/{course}/강의목록/강의목록.md")
    assert len(set(names)) == 3, names
