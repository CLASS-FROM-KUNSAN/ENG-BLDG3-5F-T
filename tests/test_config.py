"""설정 로드와 검증."""

from __future__ import annotations

import pytest

from syncnote import config as cfgmod


def test_example_config_is_valid(config_file):
    """config.example.toml 은 최소 수정만으로 통과해야 합니다.

    예시 파일이 깨지면 모든 신규 사용자가 막힙니다.
    """
    cfg = cfgmod.load(config_file())
    assert cfg.get("github.tag_strategy") == "monthly"
    assert cfg.get("github.tag_strategy_eclass") == "daily"
    assert cfg.get("recording.audio.track_layout") == "separate"


def test_defaults_fill_missing_keys(tmp):
    """설정 파일에 없는 키는 기본값으로 채워집니다."""
    path = tmp / "tiny.toml"
    path.write_text(
        '[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n',
        encoding="utf-8",
    )
    cfg = cfgmod.load(path)
    assert cfg.get("recording.video.fps") == 10
    assert cfg.get("transcribe.batch.model") == "small"
    assert cfg.get("recording.audio.silence_warn_sec") == 60


def test_missing_file_message(tmp):
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(tmp / "nope.toml")
    assert "없습니다" in str(exc.value)


def test_toml_syntax_error_is_explained(tmp):
    """TOML 문법 오류가 생 예외로 새어 나오면 안 됩니다."""
    path = tmp / "bad.toml"
    path.write_text("x = 1\nx = 2\n", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    message = str(exc.value)
    assert "TOML" in message
    assert "같은 키를 두 번" in message


def test_single_backslash_path_is_explained(tmp):
    """윈도우 경로를 \\ 하나로 쓴 흔한 실수."""
    path = tmp / "bad.toml"
    path.write_text('[general]\ndata_dir = "C:\\users\\me"\n', encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    assert "역슬래시" in str(exc.value)


@pytest.mark.parametrize(
    "edit,fragment",
    [
        (('tag_strategy = "monthly"', 'tag_strategy = "hourly"'), "tag_strategy"),
        (('track_layout = "separate"', 'track_layout = "stereo"'), "track_layout"),
        (("fps = 10", "fps = 0"), "fps"),
        (("split_size_mb = 1900", "split_size_mb = 5000"), "2000"),
        (("request_delay_sec = 1.0", "request_delay_sec = 0.05"), "request_delay_sec"),
        (('formats = ["txt", "srt"]', 'formats = ["txt", "docx"]'), "docx"),
        (('container = "mkv"', 'container = "zip"'), "container"),
    ],
)
def test_invalid_values_rejected(config_file, edit, fragment):
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(config_file([edit]))
    assert fragment in str(exc.value)


def test_all_errors_reported_at_once(config_file):
    """하나 고치고 다시 돌리기를 반복하지 않도록 전부 모아서 보고합니다."""
    path = config_file([
        ('tag_strategy = "monthly"', 'tag_strategy = "hourly"'),
        ("fps = 10", "fps = 0"),
        ('track_layout = "separate"', 'track_layout = "stereo"'),
    ])
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    assert "설정 오류 3건" in str(exc.value)


def test_timetable_validation(tmp):
    path = tmp / "tt.toml"
    path.write_text(
        '[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n'
        '[[recording.timetable]]\nname="x"\nday="wensday"\nstart="25:99"\nend="ten"\n',
        encoding="utf-8",
    )
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    message = str(exc.value)
    assert "day" in message and "start" in message and "end" in message


def test_audio_all_off_rejected(config_file):
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(config_file([
            ("system_enabled = true", "system_enabled = false"),
            ("mic_enabled = true", "mic_enabled = false"),
        ]))
    assert "system/mic" in str(exc.value)


def test_warnings_do_not_block(config_file):
    """위험하지만 유효한 설정은 통과시키고 경고만 남깁니다."""
    cfg = cfgmod.load(config_file([
        ('container = "mkv"', 'container = "mp4"'),
        ("segment_minutes = 30", "segment_minutes = 0"),
        ("delete_local_after_upload = false", "delete_local_after_upload = true"),
    ]))
    notes = " ".join(cfgmod.warnings_for(cfg))
    assert "mp4" in notes
    assert "segment_minutes = 0" in notes
    assert "삭제" in notes


def test_sensitive_upload_warning(config_file):
    """성적을 수집하면서 업로드 제외를 지우면 경고해야 합니다."""
    cfg = cfgmod.load(config_file([
        ("grade          = false", "grade          = true"),
        ('exclude_menus = ["grade", "attendance", "progress"]', "exclude_menus = []"),
    ]))
    notes = " ".join(cfgmod.warnings_for(cfg))
    assert "grade" in notes


def test_data_dir_resolution(tmp, monkeypatch):
    path = tmp / "c.toml"
    path.write_text('[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp / "appdata"))
    cfg = cfgmod.load(path)
    assert cfg.data_dir == (tmp / "appdata" / "sync-note").resolve()


def test_dotted_get_and_missing_key(config_file):
    cfg = cfgmod.load(config_file())
    assert cfg.get("recording.video.fps") == 10
    assert cfg.get("nope.nope", "fallback") == "fallback"
    with pytest.raises(cfgmod.ConfigError):
        cfg["nope.nope"]


# ---------------------------------------------------------------------------
# 문서와 코드가 같은 것을 말하는지
#
# "검사 없는 약속은 낡는다." config.example.toml 은 사용자가 읽는 문서이고
# config.py 의 DEFAULTS 는 코드가 쓰는 값입니다. 둘이 어긋나면 사용자는
# 있지도 않은 키를 적거나, 실제로 동작하는 키를 모르게 됩니다.
# ---------------------------------------------------------------------------
def _flatten(data, prefix=""):
    out = set()
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out |= _flatten(value, f"{path}.")
        else:
            out.add(path)
    return out


def _example_keys(text: str) -> set[str]:
    cfg = cfgmod.Config(data=_parse_toml(text))
    return _flatten(cfg.data)


def _parse_toml(text: str) -> dict:
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    return tomllib.loads(text)


def test_example_file_has_no_unknown_keys(example_config_text):
    """예시 파일에만 있고 코드가 모르는 키 = 사용자가 적어도 무시되는 설정."""
    example = _example_keys(example_config_text)
    known = _flatten(cfgmod.DEFAULTS)
    # 시간표는 배열이라 평탄화 대상이 아닙니다.
    unknown = {k for k in example - known if not k.startswith("recording.timetable")}
    assert not unknown, f"예시 파일에만 있는 키(코드가 무시함): {sorted(unknown)}"


def test_every_default_is_documented(example_config_text):
    """코드에만 있고 예시 파일에 없는 키 = 사용자가 존재를 모르는 설정."""
    example = _example_keys(example_config_text)
    known = _flatten(cfgmod.DEFAULTS)
    # menu_ids 는 비워 두는 것이 정상이라 예시에 값이 없습니다.
    undocumented = {
        k for k in known - example
        if not k.startswith("eclass.menu_ids")
        # timetable 은 [[배열]] 이라 비어 있으면 키 자체가 없습니다.
        # 예시 파일은 주석으로 설명합니다.
        and k != "recording.timetable"
    }
    assert not undocumented, (
        "예시 파일에 설명이 없는 설정 키: " + ", ".join(sorted(undocumented))
    )


def test_example_values_match_code_defaults(example_config_text):
    """같은 키인데 기본값이 다르면, 문서를 읽고 예상한 동작과 달라집니다."""
    example = cfgmod.Config(data=_parse_toml(example_config_text))
    defaults = cfgmod.Config(data=cfgmod.DEFAULTS)

    mismatched = []
    for key in sorted(_flatten(cfgmod.DEFAULTS)):
        if key.startswith("eclass.menu_ids"):
            continue
        sentinel = object()
        in_example = example.get(key, sentinel)
        if in_example is sentinel:
            continue
        in_code = defaults.get(key)
        # 예시 파일은 소유자/저장소처럼 사람이 채우는 칸을 미리 적어 둡니다.
        if key in {"github.owner", "github.repo", "eclass.base_url", "eclass.login_url"}:
            continue
        if in_example != in_code:
            mismatched.append(f"{key}: 예시={in_example!r} 코드={in_code!r}")

    assert not mismatched, "문서와 코드의 기본값이 다릅니다:\n  " + "\n  ".join(mismatched)


def test_workflow_substitution_targets_exist(example_config_text):
    """회귀 방지: Actions 워크플로가 예시 파일의 문자열을 치환해
    config.toml 을 만듭니다. str.replace 는 대상이 없어도 예외 없이
    원본을 돌려주므로, 예시 파일이 바뀌면 **녹화·전사가 켜진 채로**
    러너가 돌게 됩니다. 치환 대상이 사라졌는지 여기서 먼저 잡습니다.
    """
    required = [
        'username = ""',
        'data_dir = ""',
        'machine_label = ""',
        "[recording]\nenabled = true",
        "[transcribe]\nenabled = true",
    ]
    missing = [t for t in required if t not in example_config_text]
    assert not missing, (
        "워크플로가 찾는 문자열이 예시 파일에 없습니다: "
        f"{missing}\n.github/workflows/eclass-sync.yml 의 치환부도 같이 고치세요."
    )


@pytest.mark.parametrize("value, needle", [
    ('"*.pem"', "문자열 목록"),          # 문자열 하나 -> list() 가 글자로 쪼갬
    ('["*.pem", "*"]', "모든 파일"),
    ('["?*"]', "모든 파일"),             # 3차 검토: 글자 목록으로는 빠져나감
    ('["*?"]', "모든 파일"),
    ('["[!~]*"]', "모든 파일"),
    ('["*.*"]', "모든 파일"),            # 4차 검토: 확장자 있는 이름 전부
    ('["?*.*"]', "모든 파일"),
    ('["*.?*"]', "모든 파일"),
])
def test_home_exclude_names_that_would_exclude_everything_are_rejected(tmp, value, needle):
    """재검토 D: 'exclude_names = "*.pem"' 이면 아무것도 안 올리고 성공(exit 0)이었습니다."""
    path = tmp / "ex.toml"
    path.write_text('[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n'
                    f'[backup.home]\nexclude_names = {value}\n', encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError) as exc:
        cfgmod.load(path)
    assert needle in str(exc.value)


@pytest.mark.parametrize("value", ['["*.tmp", "~$*", "*.pem"]', '[]', '["node_modules"]'])
def test_ordinary_home_exclude_patterns_are_accepted(tmp, value):
    path = tmp / "ok.toml"
    path.write_text('[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n'
                    f'[backup.home]\nexclude_names = {value}\n', encoding="utf-8")
    cfgmod.load(path)
