"""설정 로드와 검증."""

from __future__ import annotations

import pytest

from gymhall import config as cfgmod


def test_example_config_is_valid(config_file):
    """config.example.toml 은 최소 수정만으로 통과해야 합니다.

    예시 파일이 깨지면 모든 신규 사용자가 막힙니다.
    """
    cfg = cfgmod.load(config_file())
    assert cfg.get("github.tag_strategy") == "monthly"
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
        ('exclude_menus = ["grade", "attendance"]', "exclude_menus = []"),
    ]))
    notes = " ".join(cfgmod.warnings_for(cfg))
    assert "grade" in notes


def test_data_dir_resolution(tmp, monkeypatch):
    path = tmp / "c.toml"
    path.write_text('[github]\nowner="o"\nrepo="r"\n[eclass]\nenabled=false\n', encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp / "appdata"))
    cfg = cfgmod.load(path)
    assert cfg.data_dir == (tmp / "appdata" / "gym-w-hall-2f").resolve()


def test_dotted_get_and_missing_key(config_file):
    cfg = cfgmod.load(config_file())
    assert cfg.get("recording.video.fps") == 10
    assert cfg.get("nope.nope", "fallback") == "fallback"
    with pytest.raises(cfgmod.ConfigError):
        cfg["nope.nope"]
