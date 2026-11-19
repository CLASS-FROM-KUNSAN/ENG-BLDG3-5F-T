"""config.toml 로드, 검증, 기본값 병합."""

from __future__ import annotations

import os
import re
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
        # e-class 자료는 날짜별 릴리스(eclass-2026-11-13)로 올립니다.
        # 언제 받은 자료인지 릴리스 이름만 보고 알 수 있어야 합니다.
        "tag_strategy_eclass": "daily",
        "fixed_tag": "archive",
        "tag_prefix_eclass": "eclass",
        "tag_prefix_recording": "recordings",
        "draft": False,
        "prerelease": False,
        "split_size_mb": 1900,
        "retry_count": 4,
        "retry_backoff": 2.0,
        "delete_local_after_upload": False,
        # 레포는 학기마다 새로 만듭니다. 없는 레포를 코드가 직접 만들지는
        # 기본적으로 않습니다 — 레포 생성은 토큰에 더 센 권한을 요구하고
        # 조직 정책으로 막혀 있는 경우가 흔합니다. 켜면 CloneUp 과 같은
        # 방식으로 만들되 auto_init 은 켭니다(릴리스 태그에 기본 브랜치 필요).
        "create_missing_repos": False,
        "repo_private": True,
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
            "account": True,   # 계정 레벨 메뉴(학습시간표·도우미 공지/FAQ 등), 개인정보 마스킹
        },
        "message_boxes": ["RECEIVE"],
        # 텍스트(.md) 옆에 같은 이름의 화면 스크린샷(.png)을 남깁니다.
        "screenshots": True,
        # 텍스트(.md) 옆에 '한 파일짜리' 전체 HTML(.html)도 남깁니다.
        # 렌더된 DOM + 이미지/CSS/폰트 인라인(SingleFile 방식), 개인정보 마스킹.
        # 과제/첨부 파일 다운로드는 이와 별개로 그대로 동작합니다.
        "capture_html": True,
        # 과목 = 레포지터리. 비우면 예전처럼 github.owner/repo 한 곳으로
        # 전부 올립니다. 적기 시작하면 그 목록이 이 PC 의 담당 전부입니다.
        # (수집 대상을 고르는 eclass.courses 와는 다른 것입니다 —
        #  저쪽은 '무엇을 받을지', 이쪽은 '어디로 올릴지'.)
        "course_repos": [],
        "risky": {
            "i_understand_the_risk": False,
            "exam": False,
            "survey": False,
            "discussion": False,
        },
        "upload": {
            "enabled": True,
            # 대상 저장소가 public 이면 올리지 않습니다(동급생 이름·화면·파일명
            # 등 개인정보가 섞일 수 있음). 정말 공개하려면 false 로 끄세요.
            "block_public": True,
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
            "monitor": 0,
            # 모니터가 여러 개일 때 각각 독립 파일로 녹화합니다.
            # 예: [1, 2]  -> <stem>_mon1.mkv, <stem>_mon2.mkv
            # 비우면([]) monitor 단수 설정을 그대로 씁니다.
            "monitors": [],
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
            # 모든 트랙이 동시에 이만큼 무음이면 멈춥니다(예약·감지 녹화만).
            # 수동 녹화는 경고만 합니다 — 사용자가 직접 시작한 것을
            # 코드가 멈추면 안 됩니다.
            "silence_stop_sec": 900,
            "silence_abort_sec": 3600,
            # '완전 무음' 기준. 기본 1e-4 는 너무 관대해서 아주 작은
            # 잡음에도 '소리가 있다' 가 됩니다.
            "silence_peak": 1e-5,
            "mic_auto_select": True,
            "mic_probe_sec": 1.5,
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
        # 터미널 녹음기(FFmpeg / SoX / arecord). 소리만 받습니다.
        # `python -m gymhall audio record` 와 cron·작업 스케줄러용입니다.
        "terminal": {
            "backend": "auto",
            "source": "mic",
            "device": "",
            "seconds": 3600,
            "format": "wav",
            "filename": "record_%Y%m%d_%H%M%S",
            "out_dir": "",
            "samplerate": 44100,
            "channels": 2,
            "vad": False,
            "vad_threshold": "3%",
            "vad_start_sec": 0.1,
            "vad_stop_sec": 3.0,
        },
    },
    # 실시간 오디오 송출 (Tailscale 망 안에서만). `python -m gymhall stream start`
    # 각 항목의 뜻은 config.example.toml 과 docs/STREAMING.md 에 있습니다.
    "stream": {
        "server": "builtin",
        "source": "system",
        "capture": "auto",
        "device": "",
        "codec": "mp3",
        "bitrate_kbps": 96,
        "samplerate": 48000,
        "channels": 2,
        "port": 8765,
        "bind": "",
        "path": "/live",
        "player_page": True,
        "title": "실시간 송출",
        "max_listeners": 0,
        "client_buffer_kb": 2048,
        "require_token": True,
        "token": "",
        "token_bytes": 16,
        # 멈춤 조건은 기본으로 전부 '없음'. 최대 송출 시간은 멈추지 않고 롤백(순환)합니다.
        "max_duration_minutes": 0,
        "on_max_duration": "rollback",
        "idle_stop_minutes": 0,
        "min_uptime_sec": 5,
        "restart_on_failure": 0,
        "restart_backoff_sec": 5,
        "restart_backoff_max_sec": 120,
        "fallback_default_device": True,
        # 시간표(recording.timetable)가 최우선
        "follow_timetable": True,
        "timetable_slots": [],
        "pre_roll_minutes": 0,
        "post_roll_minutes": 0,
        "tailscale": {
            "enabled": True,
            "mode": "serve",
            "https_port": 443,
            "use_magicdns": True,
            "allow_funnel": False,
            "exe": "",
            "link_check_sec": 60,
            "require_online": True,
            "override_existing": False,
            "teardown": True,
        },
        "icecast": {
            "host": "127.0.0.1",
            "port": 8000,
            "mount": "/live",
            "user": "source",
        },
        "issue": {
            "enabled": True,
            "owner": "",
            "repo": "",
            "title": "[실시간 송출] {label} — {started}",
            "body": "",
            "labels": ["live-stream"],
            "label_color": "d93f0b",
            "assignees": [],
            "mentions": [],
            "include_token": True,
            "on_change": "new",
            "close_previous": True,
            "comment_on_stop": True,
            "close_on_stop": True,
            "dedupe": True,
            "require_private_repo": True,
            "comment_on_rollback": False,
        },
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
    # 강의실 PC 의 홈 디렉터리 백업. 기본은 꺼져 있습니다 — Actions 러너가
    # 예시 설정으로 돌 때 러너의 홈을 올리면 안 됩니다.
    "backup": {
        "home": {
            "enabled": False,
            "root": "",              # 비우면 %USERPROFILE%
            "tag_prefix": "home",    # 릴리스 home-2026-11-13
            "bundle_mb": 1800,
            "exclude_names": [
                "ntuser.*",
                "usrclass.dat*",
                "PaceKeyChain",
                ".env",
                ".env.*",
                "*.pem",
                "*.key",
                "*.pfx",
                "*.p12",
                "id_rsa*",
                "id_ecdsa*",
                "id_ed25519*",
                "id_dsa*",
                "*.kdbx",
                "config.toml",
            ],
            "prune": {
                "enabled": False,
                "min_age_hours": 24,
            },
        },
    },
    # 활동 기록 — PDF 폼 **필드 값**(키 입력 아님)을 봉인 저장. 전용 기기 전용.
    # 기본 전부 꺼짐. 자세한 내용은 docs/ACTIVITY.md.
    "activity": {
        "enabled": False,
        "machine_is_private": False,     # 전용 기기 아니면 내용 캡처 0
        "follow_timetable": True,
        "timetable_slots": [],
        "keys_level": "field_commit",    # field_commit | document_save | field_debounce
        "debounce_ms": 800,
        # 캡처 지점(어댑터). 안 쓰는 경로는 기능이 아니라 공격면이라 기본 꺼짐.
        "adapters": {
            "pdfjs": False,
            "acrobat_web": False,
            "acrobat_js": False,
            "acrobat_uia": False,
        },
        # 웹 어댑터 허용 오리진. Adobe 기본 포함, PDF.js 데모는 제외.
        "origins": ["https://acrobat.adobe.com", "https://documentcloud.adobe.com"],
        "process_allowlist": ["Acrobat.exe"],  # 데스크톱 어댑터 프로세스 스코프
        # 정책 엔진 — 자격증명/민감 필드 제외 + 저장 전 레드액션
        "policy": {
            "drop_password": True,       # /Ff bit13 · type=password · IsPassword. 고정
            "exclude_signature": True,   # /FT /Sig
            "exclude_richtext": False,
            "name_blacklist": ["*ssn*", "*card*", "*cvv*", "*주민*", "*계좌*",
                               "*password*", "*secret*"],
            "redact_patterns": True,     # 저장 전 카드·주민번호 가림(완벽 아님)
        },
        # 하드웨어/OS 게이트 — 하나라도 실패하면 저장 거부(fail-closed)
        "gate": {
            "require_tpm2": True,
            "require_secure_boot": True,
            "require_vbs_hvci": True,
            "require_not_elevated": True,
            "require_no_debugger": True,
            "require_attestation": False,  # EK/quote. 켜면 미구현이라 fail-closed
            "read_enforces_gate": True,    # 대칭 차단. False 면 읽기는 경고 후 허용
        },
        # 저장·봉인
        "store": {
            "db_path": "",               # 비우면 <데이터>/activity/activity.db
            "dek_path": "",              # 비우면 <데이터>/activity/dek.bin (DPAPI 봉인)
            "public_key": "",
            "key_storage": "tpm",        # 추출 불가. TPM 없으면 저장 안 됨
            "require_hello": True,
            "recovery_key": False,       # 복구 불가 수용
            "retention_days": 0,
        },
        # C(데스크톱 Acrobat) 사이드카 워처. acrobat_js 어댑터가 떨군 필드 값을 읽음.
        "watch_acrobat": {
            "staging_dir": "",           # 비우면 <데이터>/activity/.staging
            "poll_sec": 1.0,
            "sweep_minutes": 5,          # 기동 시 이보다 묵은 사이드카는 삭제(평문 잔존 방지)
        },
        "upload": "sealed_only",         # off | sealed_only
        "pause_hotkey": "Ctrl+Alt+P",
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
_VALID_TERMINAL_BACKENDS = {"auto", "ffmpeg", "sox", "arecord"}
_VALID_TERMINAL_SOURCES = {"mic", "system"}
_VALID_TERMINAL_FORMATS = {"wav", "mp3", "flac"}
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


# 이름 몇 개를 넣어 보고 전부 맞으면 '모든 파일' 패턴입니다. 글자 목록으로
# 막으면 '?*' / '[!~]*' 같은 것이 빠져나갑니다(재검토). 스캔과 같은 규칙
# (소문자, fnmatchcase)으로 봅니다.
# '확장자 있는 이름 전부' 도 사실상 전부입니다('*.*' — 4차 검토).
_EXCLUDE_PROBES = ("a", "x.txt", "documents", "b c.pdf", "과제.hwp", "1")
_EXCLUDE_PROBES_EXT = ("x.txt", "b c.pdf", "과제.hwp", "report.final.docx", "a.b")


def _matches_everything(pattern: str) -> bool:
    import fnmatch

    pat = str(pattern).strip().lower()
    if not pat:
        return False
    return any(all(fnmatch.fnmatchcase(n, pat) for n in probes)
               for probes in (_EXCLUDE_PROBES, _EXCLUDE_PROBES_EXT))


def _validate_stream(cfg: Config, errors: list[str]) -> None:
    _choice(cfg, "stream.server", {"builtin", "icecast"}, errors)
    _choice(cfg, "stream.source", {"system", "mic"}, errors)
    _choice(cfg, "stream.capture", {"auto", "soundcard", "ffmpeg"}, errors)
    _choice(cfg, "stream.codec", {"mp3", "aac"}, errors)
    _choice(cfg, "stream.tailscale.mode", {"direct", "serve", "funnel"}, errors)
    _choice(cfg, "stream.issue.on_change", {"new", "comment", "none"}, errors)
    _choice(cfg, "stream.on_max_duration", {"rollback", "stop"}, errors)
    for key in ("bitrate_kbps", "samplerate", "channels", "client_buffer_kb", "token_bytes",
                "restart_backoff_sec", "restart_backoff_max_sec"):
        _positive(cfg, f"stream.{key}", errors)
    for key in ("max_listeners", "max_duration_minutes", "idle_stop_minutes",
                "min_uptime_sec", "restart_on_failure", "pre_roll_minutes", "post_roll_minutes"):
        _positive(cfg, f"stream.{key}", errors, allow_zero=True)
    slots = cfg.get("stream.timetable_slots", [])
    if not isinstance(slots, list) or not all(isinstance(v, str) for v in slots):
        errors.append('stream.timetable_slots 는 수업 이름 목록이어야 합니다 (예: ["자료구조"])')
    elif slots:
        known = {str(e.get("name", "")) for e in cfg.get("recording.timetable", []) or []
                 if isinstance(e, dict)}
        missing = [n for n in slots if n not in known]
        if missing:
            errors.append(f"stream.timetable_slots 의 {missing} 가 [[recording.timetable]] 에 없습니다")
    _positive(cfg, "stream.tailscale.link_check_sec", errors, allow_zero=True)
    for key in ("stream.port", "stream.tailscale.https_port", "stream.icecast.port"):
        port = cfg.get(key)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            errors.append(f"{key} 는 1~65535 사이 정수여야 합니다 (현재: {port!r})")
    for key in ("stream.path", "stream.icecast.mount"):
        if not str(cfg.get(key, "")).startswith("/"):
            errors.append(f"{key} 는 '/' 로 시작해야 합니다 (현재: {cfg.get(key)!r})")
    if str(cfg.get("stream.path", "")) in {"/", "/status"}:
        errors.append("stream.path 는 '/' 나 '/status' 가 될 수 없습니다(재생 페이지·상태가 씁니다)")
    token = str(cfg.get("stream.token", ""))
    if token and len(token) < 12:
        errors.append("stream.token 이 12자보다 짧습니다. 비우면 송출마다 무작위로 만듭니다.")
    if not re.fullmatch(r"#?[0-9a-fA-F]{6}", str(cfg.get("stream.issue.label_color", ""))):
        errors.append("stream.issue.label_color 는 6자리 16진수 색이어야 합니다 (예: 'd93f0b')")
    for key in ("labels", "assignees", "mentions"):
        value = cfg.get(f"stream.issue.{key}", [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"stream.issue.{key} 는 문자열 목록이어야 합니다")
    if (cfg.get("stream.tailscale.mode") == "funnel"
            and not cfg.get("stream.tailscale.allow_funnel")):
        errors.append(
            'stream.tailscale.mode = "funnel" 은 인터넷 전체에 공개합니다. '
            "정말 필요하면 stream.tailscale.allow_funnel = true 를 함께 적으세요."
        )
    if (cfg.get("stream.tailscale.mode") == "funnel"
            and cfg.get("stream.tailscale.https_port") not in {443, 8443, 10000}):
        errors.append("funnel 의 https_port 는 443, 8443, 10000 중 하나여야 합니다 (Tailscale 제한)")


def _validate_activity(cfg: Config, errors: list[str]) -> None:
    _choice(cfg, "activity.keys_level",
            {"field_commit", "document_save", "field_debounce"}, errors)
    _choice(cfg, "activity.upload", {"off", "sealed_only"}, errors)
    _choice(cfg, "activity.store.key_storage", {"tpm"}, errors)
    _positive(cfg, "activity.debounce_ms", errors)
    _positive(cfg, "activity.store.retention_days", errors, allow_zero=True)
    _positive(cfg, "activity.watch_acrobat.poll_sec", errors)
    _positive(cfg, "activity.watch_acrobat.sweep_minutes", errors, allow_zero=True)

    for key in ("activity.origins", "activity.timetable_slots",
                "activity.process_allowlist", "activity.policy.name_blacklist"):
        value = cfg.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            errors.append(f"{key} 는 문자열 목록이어야 합니다")

    # 비밀번호 제외는 끌 수 없습니다 — 최소 수집 원칙의 핵심.
    if not cfg.get("activity.policy.drop_password", True):
        errors.append(
            "activity.policy.drop_password 는 끌 수 없습니다(True 고정). "
            "비밀번호 필드는 담지 않는 것이 이 기능의 전제입니다."
        )
    # 전용 기기 전제가 깨진 상태에서 복구키까지 없으면 데이터는 영구 복구 불가.
    # (수용한 선택이지만, 켜진 채 recovery_key=false 는 경고로 남깁니다 → warnings_for)


def validate(cfg: Config) -> None:
    """잘못된 설정을 한 번에 모아서 알려줍니다."""
    errors: list[str] = []
    _validate_stream(cfg, errors)
    _validate_activity(cfg, errors)

    _choice(cfg, "github.tag_strategy", _VALID_TAG_STRATEGIES, errors)
    _choice(cfg, "github.tag_strategy_eclass", _VALID_TAG_STRATEGIES, errors)
    _choice(cfg, "recording.audio.track_layout", _VALID_TRACK_LAYOUTS, errors)
    _choice(cfg, "recording.video.backend", _VALID_VIDEO_BACKENDS, errors)
    _choice(cfg, "recording.audio.backend", _VALID_AUDIO_BACKENDS, errors)
    _choice(cfg, "recording.terminal.backend", _VALID_TERMINAL_BACKENDS, errors)
    _choice(cfg, "recording.terminal.source", _VALID_TERMINAL_SOURCES, errors)
    _choice(cfg, "recording.terminal.format", _VALID_TERMINAL_FORMATS, errors)
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
    _positive(cfg, "backup.home.bundle_mb", errors)
    _positive(cfg, "backup.home.prune.min_age_hours", errors, allow_zero=True)
    bundle_mb = cfg.get("backup.home.bundle_mb", 1800)
    if isinstance(bundle_mb, (int, float)) and bundle_mb > 1900:
        errors.append("backup.home.bundle_mb 는 1900 이하로 두세요 (릴리스 에셋 상한 2GiB).")
    if not str(cfg.get("backup.home.tag_prefix", "home")).strip():
        errors.append("backup.home.tag_prefix 가 비어 있습니다")
    # 문자열 하나로 적으면 list("*.pem") = ['*', '.', 'p', 'e', 'm'] 이 되고,
    # '*' 가 모든 이름에 맞아서 아무것도 안 올리고 성공으로 끝납니다.
    excl = cfg.get("backup.home.exclude_names", [])
    if not isinstance(excl, list) or not all(isinstance(p, str) for p in excl):
        errors.append('backup.home.exclude_names 는 문자열 목록이어야 합니다 '
                      '(예: ["*.pem", "node_modules"])')
    elif any(_matches_everything(p) for p in excl):
        errors.append("backup.home.exclude_names 에 모든 파일에 맞는 패턴('*')이 있습니다. "
                      "그러면 아무것도 백업되지 않습니다.")
    _positive(cfg, "recording.terminal.seconds", errors)
    _positive(cfg, "recording.terminal.vad_start_sec", errors, allow_zero=True)
    _positive(cfg, "recording.terminal.vad_stop_sec", errors)

    # SoX 의 silence 문턱값: 3% 처럼 백분율이거나 -40d 처럼 dB 입니다.
    threshold = str(cfg.get("recording.terminal.vad_threshold", "3%"))
    if not re.fullmatch(r"\d+(\.\d+)?%|-?\d+(\.\d+)?d", threshold):
        errors.append(
            f"recording.terminal.vad_threshold = {threshold!r} 는 '3%' 나 '-40d' 형식이어야 합니다"
        )

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

        # monitors 목록 검증: 정수 목록이어야 하고, 같은 번호가 중복되면 안 됩니다.
        monitors_val = cfg.get("recording.video.monitors", [])
        if not isinstance(monitors_val, list):
            errors.append("recording.video.monitors 는 정수 목록이어야 합니다 (예: [1, 2])")
        else:
            for i, v in enumerate(monitors_val):
                if not isinstance(v, int) or v <= 0:
                    errors.append(
                        f"recording.video.monitors[{i}] = {v!r} 는 1 이상의 정수여야 합니다"
                    )
            if len(monitors_val) != len(set(monitors_val)):
                errors.append("recording.video.monitors 에 같은 번호가 중복됩니다")

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

    if cfg.get("activity.enabled"):
        if not cfg.get("activity.machine_is_private"):
            notes.append(
                "activity.enabled = true 인데 machine_is_private = false 입니다. "
                "전용 기기가 아니면 필드 값 내용 캡처는 돌지 않습니다(어댑터는 비활성)."
            )
        adapters = cfg.section("activity.adapters")
        desktop_on = adapters.get("acrobat_js") or adapters.get("acrobat_uia")
        if desktop_on and not cfg.get("activity.machine_is_private"):
            notes.append(
                "데스크톱 Acrobat 어댑터(acrobat_js/uia)는 전용 기기(machine_is_private=true)에서만 켜집니다. "
                "공용 기기에서는 다른 사람이 채운 폼 값까지 담길 수 있어 꺼 둡니다."
            )
        if not str(cfg.get("activity.store.public_key", "")).strip():
            notes.append("activity.store.public_key 가 비어 있습니다. 봉인할 공개키를 넣기 전엔 저장되지 않습니다.")
        if not cfg.get("activity.store.recovery_key"):
            notes.append(
                "activity.store.recovery_key = false 입니다. 기기가 죽으면 활동 로그는 영구 복구 불가입니다(수용한 선택)."
            )
        if not cfg.get("activity.gate.read_enforces_gate", True):
            notes.append(
                "activity.gate.read_enforces_gate = false 입니다. 게이트 실패(Secure Boot 꺼짐 등) 상태에서도 "
                "복호가 경고만 하고 허용됩니다 — 오염 가능한 OS 에 평문이 노출될 수 있습니다."
            )

    if cfg.get("recording.segment_minutes", 0) == 0:
        notes.append(
            "segment_minutes = 0 이라 녹화가 한 파일로 저장됩니다. "
            "2GiB 를 넘으면 릴리스 업로드 시 분할되고, 중간에 뻗으면 전부 잃습니다."
        )

    if cfg.get("backup.home.enabled") and cfg.get("backup.home.exclude_names") == []:
        notes.append(
            "backup.home.exclude_names 가 비어 있습니다. 레지스트리(NTUSER.DAT), "
            "개인 키(*.pem, id_rsa) 등도 올라갑니다."
        )

    if cfg.get("backup.home.prune.enabled"):
        hours = cfg.get("backup.home.prune.min_age_hours", 24)
        notes.append(
            "홈 정리가 켜져 있습니다. GitHub 사슬로 확인된 파일은 로컬에서 지워집니다 "
            f"(올린 뒤 {hours}시간 이후)."
        )

    if (cfg.get("stream.issue.enabled") and cfg.get("stream.issue.include_token")
            and not cfg.get("stream.issue.require_private_repo")):
        notes.append(
            "stream.issue.require_private_repo = false 이고 include_token = true 입니다. "
            "public 저장소면 송출 링크와 토큰이 그대로 공개됩니다."
        )
    if cfg.get("stream.tailscale.mode") == "funnel":
        notes.append("실시간 송출이 funnel(인터넷 공개)로 설정돼 있습니다. 강의 소리라면 공중송신이 될 수 있습니다.")

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
