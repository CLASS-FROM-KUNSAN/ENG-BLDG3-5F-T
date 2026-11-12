"""config.toml 로드, 검증, 기본값 병합."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "Python 3.11 미만에서는 'tomli' 가 필요합니다: pip install tomli"
        ) from exc


class ConfigError(Exception):
    """설정이 잘못됐을 때."""


# ---------------------------------------------------------------------------
# 기본값. config.toml 에 없는 키는 여기서 채웁니다.
# ---------------------------------------------------------------------------
# 성적·출석·진도율은 본인 개인정보입니다. 수집은 선택이고, 켜더라도
# 기본적으로 릴리스에는 올리지 않습니다. 업로드 제외 목록의 기본값과
# 경고 문구가 같은 목록을 보도록 한곳에 둡니다 — 따로 적어 두면
# 둘 중 하나만 고쳐져서 조용히 어긋납니다.
SENSITIVE_MENUS: tuple[str, ...] = ("grade", "attendance", "progress")

DEFAULTS: dict[str, Any] = {
    "general": {
        "data_dir": "",
        "log_level": "INFO",
        "log_retention_days": 30,
        "machine_label": "",
    },
    "github": {
        "enabled": True,
        "owner": "",
        "repo": "",
        "tag_strategy": "monthly",
        "fixed_tag": "archive",
        "tag_prefix_eclass": "eclass",
        "tag_prefix_recording": "recordings",
        "draft": False,
        "prerelease": False,
        "split_size_mb": 1900,
        "retry_count": 4,
        "retry_backoff": 2.0,
        "delete_local_after_upload": False,
    },
    "eclass": {
        "enabled": True,
        "base_url": "",
        "login_url": "",
        "username": "",
        "headless": True,
        "timeout_ms": 30000,
        "request_delay_sec": 1.0,
        "courses": [],
        "exclude_courses": [],
        "menus": {
            "curriculum": True,
            "lecture_list": True,
            "syllabus": True,
            "course_info": True,
            "notice": True,
            "qna": True,
            "materials": True,
            "assignment": True,
            "attendance": False,
            "team": False,
            "progress": False,
            "grade": False,
            "message": True,
        },
        "message_boxes": ["RECEIVE"],
        "risky": {
            "i_understand_the_risk": False,
            "exam": False,
            "survey": False,
            "discussion": False,
        },
        "upload": {
            "enabled": True,
            "exclude_menus": list(SENSITIVE_MENUS),
            "allow_extensions": [],
            "max_file_mb": 0,
        },
        "menu_ids": {},
    },
    "recording": {
        "enabled": True,
        "container": "mkv",
        "segment_minutes": 30,
        "max_duration_minutes": 240,
        "pre_roll_minutes": 2,
        "post_roll_minutes": 3,
        "video": {
            "enabled": True,
            "backend": "gdigrab",
            "fps": 10,
            "scale_width": 1280,
            "scale_height": 0,
            "preset": "veryfast",
            "crf": 30,
            "window_title": "",
            "draw_mouse": True,
        },
        "audio": {
            "enabled": True,
            "backend": "soundcard",
            "samplerate": 48000,
            "channels": 2,
            "system_enabled": True,
            "system_device": "",
            "system_gain_db": 0.0,
            "mic_enabled": True,
            "mic_device": "",
            "mic_gain_db": 0.0,
            "track_layout": "separate",
            "silence_warn_sec": 60,
        },
        "triggers": {
            "manual": True,
            "schedule_enabled": False,
            "watch_enabled": False,
            # 예시 파일(config.example.toml)과 같은 값이어야 합니다.
            # 다르면 사용자가 이 줄을 지웠을 때 감시가 조용히 아무것도
            # 하지 않게 됩니다. tests/test_config.py 가 둘을 대조합니다.
            "watch_processes": ["Zoom.exe", "Webex.exe", "Teams.exe"],
            "watch_window_titles": [],
            "watch_interval_sec": 15,
            "watch_stop_grace_sec": 60,
        },
        "timetable": [],
    },
    "transcribe": {
        "enabled": True,
        "engine": "faster-whisper",
        "language": "ko",
        "formats": ["txt", "srt"],
        "source_track": "system",
        "batch": {
            "enabled": True,
            "model": "small",
            "device": "auto",
            "compute_type": "int8",
            "cpu_threads": 0,
            "mode": "queue",
            "vad_filter": True,
        },
        "realtime": {
            "enabled": False,
            "model": "tiny",
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
            "chunk_sec": 8,
            "overlap_sec": 1.5,
            "print_to_console": True,
        },
        "cleanup": {
            "enabled": True,
            "drop_hallucinations": True,
            "no_speech_threshold": 0.85,
            "max_repeats": 3,
            "extra_phrases": [],
            "use_logprob_rule": True,
        },
        "openai": {
            "model": "whisper-1",
            "chunk_minutes": 10,
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """base 를 복사해 override 값을 재귀적으로 덮어씁니다."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    """설정 전체. 점 표기 경로로 읽습니다: cfg.get('recording.video.fps')"""

    data: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # -- 조회 ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted, {})
        return value if isinstance(value, dict) else {}

    def __getitem__(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"설정 키를 찾을 수 없습니다: {dotted}")
        return value

    # -- 경로 ---------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        raw = str(self.get("general.data_dir", "")).strip()
        if raw:
            return Path(os.path.expandvars(raw)).expanduser().resolve()
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
        if base:
            return Path(base).expanduser().resolve() / "gym-w-hall-2f"
        return Path.home() / ".gym-w-hall-2f"

    @property
    def machine_label(self) -> str:
        raw = str(self.get("general.machine_label", "")).strip()
        if raw:
            return raw
        import platform

        return platform.node() or "unknown-pc"


def default_config_path() -> Path:
    """config.toml 을 찾는 순서."""
    env = os.environ.get("GYMHALL_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    # 레포 루트 (src/gymhall/config.py -> ../../..)
    return (Path(__file__).resolve().parents[2] / "config.toml").resolve()


def load(path: str | Path | None = None) -> Config:
    """설정을 읽어 기본값과 병합하고 검증합니다."""
    cfg_path = Path(path).expanduser().resolve() if path else default_config_path()

    if not cfg_path.exists():
        example = cfg_path.with_name("config.example.toml")
        hint = f"\n  {example} 를 {cfg_path.name} 으로 복사한 뒤 수정하세요." if example.exists() else ""
        raise ConfigError(f"설정 파일이 없습니다: {cfg_path}{hint}")

    try:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"{cfg_path} 의 TOML 문법이 잘못됐습니다.\n"
            f"  {exc}\n"
            "흔한 원인:\n"
            "  - 같은 키를 두 번 썼습니다\n"
            "  - 문자열에 따옴표를 빠뜨렸습니다\n"
            '  - 경로의 역슬래시를 하나만 썼습니다 (C:\\\\폴더 처럼 두 번 쓰거나 / 를 쓰세요)\n'
            "  - [섹션] 이름을 두 번 선언했습니다"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽을 수 없습니다: {cfg_path}\n  {exc}") from exc

    merged = _deep_merge(DEFAULTS, raw)
    cfg = Config(data=merged, path=cfg_path)
    validate(cfg)
    return cfg


# ---------------------------------------------------------------------------
# 검증
# ---------------------------------------------------------------------------
_VALID_TAG_STRATEGIES = {"monthly", "weekly", "daily", "single"}
_VALID_TRACK_LAYOUTS = {"separate", "mixed", "both"}
_VALID_VIDEO_BACKENDS = {"gdigrab", "ddagrab"}
_VALID_AUDIO_BACKENDS = {"soundcard", "dshow"}
_VALID_ENGINES = {"faster-whisper", "openai"}
_VALID_BATCH_MODES = {"immediate", "queue"}
_VALID_SOURCE_TRACKS = {"system", "mic", "both"}
_VALID_FORMATS = {"txt", "srt", "vtt", "json"}
_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


def _choice(cfg: Config, key: str, allowed: set[str], errors: list[str]) -> None:
    value = cfg.get(key)
    if value is not None and value not in allowed:
        errors.append(f"{key} = {value!r} 은(는) 허용되지 않습니다. 가능: {sorted(allowed)}")


def _positive(cfg: Config, key: str, errors: list[str], allow_zero: bool = False) -> None:
    value = cfg.get(key)
    if not isinstance(value, (int, float)):
        errors.append(f"{key} 는 숫자여야 합니다 (현재: {value!r})")
        return
    if value < 0 or (value == 0 and not allow_zero):
        errors.append(f"{key} 는 {'0 이상' if allow_zero else '0보다 큰 값'}이어야 합니다 (현재: {value})")


def validate(cfg: Config) -> None:
    """잘못된 설정을 한 번에 모아서 알려줍니다."""
    errors: list[str] = []

    _choice(cfg, "github.tag_strategy", _VALID_TAG_STRATEGIES, errors)
    _choice(cfg, "recording.audio.track_layout", _VALID_TRACK_LAYOUTS, errors)
    _choice(cfg, "recording.video.backend", _VALID_VIDEO_BACKENDS, errors)
    _choice(cfg, "recording.audio.backend", _VALID_AUDIO_BACKENDS, errors)
    _choice(cfg, "transcribe.engine", _VALID_ENGINES, errors)
    _choice(cfg, "transcribe.batch.mode", _VALID_BATCH_MODES, errors)
    _choice(cfg, "transcribe.source_track", _VALID_SOURCE_TRACKS, errors)

    if cfg.get("recording.container") not in {"mkv", "mp4"}:
        errors.append("recording.container 는 'mkv' 또는 'mp4' 여야 합니다")

    bad_formats = set(cfg.get("transcribe.formats", [])) - _VALID_FORMATS
    if bad_formats:
        errors.append(f"transcribe.formats 에 알 수 없는 값: {sorted(bad_formats)}")

    _positive(cfg, "github.split_size_mb", errors)
    _positive(cfg, "recording.video.fps", errors)
    _positive(cfg, "recording.max_duration_minutes", errors)
    _positive(cfg, "recording.segment_minutes", errors, allow_zero=True)
    _positive(cfg, "eclass.request_delay_sec", errors, allow_zero=True)
    _positive(cfg, "transcribe.realtime.chunk_sec", errors)

    split_mb = cfg.get("github.split_size_mb", 1900)
    if isinstance(split_mb, (int, float)) and split_mb > 2000:
        errors.append(
            "github.split_size_mb 가 2000 을 넘습니다. "
            "GitHub 릴리스 에셋 상한은 2GiB 이므로 1900 이하로 두세요."
        )

    delay = cfg.get("eclass.request_delay_sec", 1.0)
    if isinstance(delay, (int, float)) and 0 < delay < 0.5:
        errors.append("eclass.request_delay_sec 가 0.5 미만입니다. 서버 부담을 줄이기 위해 0.5 이상으로 두세요.")

    if cfg.get("github.enabled"):
        if not str(cfg.get("github.owner", "")).strip():
            errors.append("github.owner 가 비어 있습니다")
        if not str(cfg.get("github.repo", "")).strip():
            errors.append("github.repo 가 비어 있습니다")

    if cfg.get("eclass.enabled"):
        if not str(cfg.get("eclass.login_url", "")).strip():
            errors.append("eclass.login_url 이 비어 있습니다")
        if not str(cfg.get("eclass.username", "")).strip():
            errors.append("eclass.username 이 비어 있습니다")

    # 시간표
    for idx, entry in enumerate(cfg.get("recording.timetable", []) or []):
        where = f"recording.timetable[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} 형식이 잘못됐습니다")
            continue
        day = str(entry.get("day", "")).lower()
        if day not in _DAYS:
            errors.append(f"{where}.day = {entry.get('day')!r} (가능: {sorted(_DAYS)})")
        lang = entry.get("language")
        if lang is not None and not isinstance(lang, (str, list)):
            errors.append(f"{where}.language 는 문자열이거나 목록이어야 합니다")
        for field_name in ("start", "end"):
            value = str(entry.get(field_name, ""))
            parts = value.split(":")
            ok = len(parts) == 2 and all(p.isdigit() for p in parts)
            if ok:
                hour, minute = int(parts[0]), int(parts[1])
                ok = 0 <= hour <= 23 and 0 <= minute <= 59
            if not ok:
                errors.append(f"{where}.{field_name} = {value!r} 는 HH:MM 형식이어야 합니다")

    # 저사양 경고가 아니라 실제 오류인 것만 여기서 막습니다.
    if cfg.get("transcribe.realtime.enabled") and not cfg.get("recording.audio.enabled"):
        errors.append("transcribe.realtime.enabled = true 인데 recording.audio.enabled = false 입니다")

    if cfg.get("recording.enabled"):
        audio = cfg.section("recording.audio")
        video = cfg.section("recording.video")
        if not audio.get("enabled") and not video.get("enabled"):
            errors.append("recording 이 켜져 있는데 video/audio 가 모두 꺼져 있습니다")
        if audio.get("enabled") and not (audio.get("system_enabled") or audio.get("mic_enabled")):
            errors.append("recording.audio 가 켜져 있는데 system/mic 가 모두 꺼져 있습니다")

    if errors:
        bullet = "\n  - ".join(errors)
        raise ConfigError(f"설정 오류 {len(errors)}건:\n  - {bullet}")


def warnings_for(cfg: Config) -> list[str]:
    """막지는 않지만 알려줘야 할 것들."""
    notes: list[str] = []

    if cfg.get("transcribe.realtime.enabled"):
        notes.append(
            "실시간 전사가 켜져 있습니다. 저사양 PC에서는 녹화 프레임이 떨어질 수 있습니다. "
            "recording.video.fps 를 5 이하로 낮추는 것을 권합니다."
        )
        if cfg.get("transcribe.realtime.model") not in {"tiny", "base"}:
            notes.append(
                f"실시간 전사 모델이 '{cfg.get('transcribe.realtime.model')}' 입니다. "
                "실시간에는 tiny 또는 base 를 쓰세요."
            )

    if cfg.get("recording.container") == "mp4":
        notes.append(
            "container = 'mp4' 입니다. 녹화 도중 전원이 꺼지면 파일 전체가 재생 불가가 됩니다. mkv 를 권합니다."
        )

    if cfg.get("recording.segment_minutes", 0) == 0:
        notes.append(
            "segment_minutes = 0 이라 녹화가 한 파일로 저장됩니다. "
            "2GiB 를 넘으면 릴리스 업로드 시 분할되고, 중간에 뻗으면 전부 잃습니다."
        )

    if cfg.get("github.delete_local_after_upload"):
        notes.append("업로드 후 로컬 원본을 삭제하도록 설정돼 있습니다. 업로드 실패 시 복구할 수 없습니다.")

    if cfg.get("eclass.risky.i_understand_the_risk"):
        on = [k for k in ("exam", "survey", "discussion") if cfg.get(f"eclass.risky.{k}")]
        if on:
            notes.append(
                f"위험 메뉴가 켜져 있습니다: {', '.join(on)}. "
                "목록 제목만 읽고 상세 페이지에는 진입하지 않지만, 접근 기록 자체가 남을 수 있습니다."
            )

    if cfg.get("eclass.upload.enabled"):
        excluded = set(cfg.get("eclass.upload.exclude_menus", []))
        for sensitive in SENSITIVE_MENUS:
            if cfg.get(f"eclass.menus.{sensitive}") and sensitive not in excluded:
                notes.append(
                    f"'{sensitive}' 수집 결과가 릴리스에 업로드됩니다. "
                    f"민감정보라면 eclass.upload.exclude_menus 에 '{sensitive}' 를 추가하세요."
                )

    return notes
