"""실패·오류를 빠짐없이 GitHub 이슈로 올립니다.

전제(사용자 요청 — 영가설): 강의실 PC 와 닿는 길이 GitHub 이슈·PR·레포뿐이라고 봅니다. 그러면
로그 파일은 아무도 못 봅니다. 그래서 녹화·녹음·업로드·업데이트(병합 충돌 포함)·작업 등록·송출·
수집이 실패하거나 오류를 내면 **하나도 빠짐없이** 이슈로 남깁니다.

구조 — 남기기와 올리기를 나눕니다(로그를 쓰는 자리를 절대 늦추거나 깨지 않게):
  1. 잡기(IncidentHandler, logging 핸들러): 로그 기록을 분류(정규화한 코드)해 이 PC 의
     state\\incidents\\queue.jsonl 에 한 줄로 덧붙입니다. 네트워크 없음, 예외 없음.
     - ERROR 이상은 전부. WARNING 은 '실패' 를 뜻하는 것만(RULES 의 warn=True).
     - 분류는 (1) 로그를 쓴 자리가 extra={"incident": "코드"} 로 직접 준 코드, (2) 아래 RULES 의
       로거·문장 규칙, (3) 둘 다 없으면 UNCLASSIFIED.<로거> — 버리지 않습니다.
     - 바로 앞 로그 40줄을 같이 남겨 무슨 일이 있었는지 볼 수 있게 합니다.
  2. 올리기(flush): 쌓인 것을 (PC, 코드) 마다 이슈 하나로 묶습니다. 열린 이슈가 있으면 댓글로 이어
     쓰고(같은 이슈에 10분에 한 번, 그 사이 것은 모아서), 닫혔거나 없으면 새로 엽니다. 실패하면
     대기열에 그대로 두고 다음에 다시 — 네트워크가 끊겨도 잃지 않습니다.
     - 길: 저장해 둔 GitHub 토큰(secrets github)의 REST, 없으면 gh CLI(gh auth login).
     - 언제: 오래 도는 명령(녹화 대기·감지·지킴이·송출)은 1분마다 뒤에서, 모든 명령은 끝날 때 한 번,
       `syncnote incidents flush` 로 직접.
  3. 가리기(redact): 올리기 전에 사용자 경로·토큰·메일·IP·7자리 이상 숫자(학번 등)·이름 칸을 가립니다.
     레포가 public 일 수 있으므로(강의실 레포) 가린 것만 올립니다.

설정 [incidents]: enabled · repo(비우면 github.owner/github.repo) · labels · min_comment_minutes ·
context_lines · max_writes_per_flush.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

LOGGER_NAME = "syncnote.incidents"
log = logging.getLogger(LOGGER_NAME)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --- 분류(정규화한 코드) -----------------------------------------------------------
# 코드 = 영역.부분.원인. severity: critical(수업 녹화·녹음을 잃었거나 잃을 수 있음) · error · warning.

@dataclass(frozen=True)
class Rule:
    code: str
    severity: str
    pattern: str                 # 메시지에 대한 정규식
    logger: str = ""             # 로거 이름 접두사(비우면 아무 로거)
    warn: bool = False           # WARNING 도 잡을지
    title: str = ""              # 사람이 읽을 이름


RULES: tuple[Rule, ...] = (
    # 디스크·시스템
    Rule("SYS.DISK.FULL", "critical", r"No space left|Errno 28|디스크.*가득", warn=True,
         title="디스크가 가득 참"),
    Rule("SYS.UNCAUGHT", "critical", r"예기치 못한 오류|처리되지 않은 예외|Unhandled exception",
         title="처리되지 않은 오류"),
    # 녹화 — 잠금·넘겨받기
    Rule("REC.LOCK.BUSY", "critical", r"녹화를 시작하지 않습니다|녹화를 시작하지 못했습니다", warn=True,
         title="다른 녹화 때문에 녹화를 시작하지 못함"),
    Rule("REC.HANDOVER.TIMEOUT", "critical", r"비키지 않았습니다", title="넘겨받기 실패"),
    Rule("REC.HANDOVER.YIELD", "warning", r"마무리하고 넘깁니다|넘겨 달라고 요청", warn=True,
         title="녹화 넘겨받기"),
    # 녹화 — 세그먼트·화면·합치기
    Rule("REC.SEGMENT.FAILED", "critical", r"세그먼트 \d+ 실패", title="녹화 세그먼트 실패"),
    Rule("REC.VIDEO.EXIT", "critical", r"ffmpeg 가 예기치 않게 종료", warn=True, title="화면 녹화 중단"),
    Rule("REC.VIDEO.EMPTY", "critical", r"영상 파일이 사실상 비어", warn=True, title="화면 녹화가 비어 있음"),
    Rule("REC.VIDEO.AUDIO_ONLY", "error", r"소리만 녹음합니다", warn=True, title="화면 없이 소리만 녹음"),
    Rule("REC.MUX.FAILED", "critical", r"합치기 실패", title="녹화 파일 합치기 실패"),
    Rule("REC.JOURNAL.FAILED", "error", r"녹화 일지를 쓰지 못", warn=True, title="녹화 일지 실패"),
    Rule("REC.FFMPEG.MISSING", "critical", r"ffmpeg 가 없", warn=True, title="ffmpeg 없음"),
    # 녹화 — 소리
    Rule("REC.AUDIO.CAPTURE", "critical", r"녹음 중 오류", title="녹음 중 오류"),
    Rule("REC.AUDIO.OPEN", "critical", r"장치를 열 수 없습니다|열지 못했습니다", warn=True,
         title="오디오 장치를 열지 못함"),
    Rule("REC.AUDIO.FAILOVER", "warning", r"녹음 중 방법을 바꿨습니다|다시 이었습니다", warn=True,
         title="녹음 방법 전환(끊김)"),
    Rule("REC.AUDIO.SILENT", "error", r"전 구간 무음", warn=True, title="트랙 전 구간 무음"),
    Rule("REC.AUDIO.ZERO", "error", r"신호가 정확히 0|비트 단위 0", warn=True, title="신호가 정확히 0"),
    Rule("REC.AUDIO.LONG_SILENCE", "warning", r"무음이 .*(분|시간).*(계속|이어)", warn=True,
         title="긴 무음"),
    Rule("REC.MIC.MIX_ONLY", "critical", r"기본 녹음 장치가 .*스테레오 믹스.*마이크 입력이 없", warn=True,
         title="마이크 미감지(스테레오 믹스뿐)"),
    Rule("REC.MIC.NONE", "error", r"실제 마이크가 없습니다|녹음 장치\(마이크\)가 하나도 없", warn=True,
         title="마이크 없음"),
    Rule("REC.MIC.PERMISSION", "critical", r"마이크 권한이 꺼져|권한이 꺼져 있어", warn=True,
         title="마이크 권한 꺼짐"),
    # 업로드·지킴이·복구
    Rule("UPLOAD.PUBLIC_BLOCKED", "error", r"public .*(막|차단)|PublicRepoBlocked", warn=True,
         title="public 레포라 업로드 막힘"),
    Rule("UPLOAD.FAILED", "error", r"업로드 실패|올리지 못했습니다|업로드.*오류", warn=True,
         title="업로드 실패"),
    Rule("GUARD.QUARANTINE", "error", r"격리", warn=True, title="녹화 파일 격리"),
    Rule("RECOVER.FAILED", "error", r"녹화 (조각 )?복구 (중 오류|실패)", warn=True, title="녹화 복구 실패"),
    Rule("RECOVER.LEFTOVER", "warning", r"끝나지 않은 녹화 조각", warn=True,
         title="녹화 중 꺼짐(조각 복구)"),
    # 업데이트(git pull)
    Rule("UPDATE.MERGE_CONFLICT", "critical", r"CONFLICT|병합 충돌|Automatic merge failed",
         warn=True, title="업데이트 병합 충돌"),
    Rule("UPDATE.FETCH_FAILED", "error", r"가져오기 실패", warn=True, title="업데이트 가져오기 실패"),
    Rule("UPDATE.PULL_FAILED", "error", r"받기 실패", warn=True, title="업데이트 받기 실패"),
    Rule("UPDATE.PIP_FAILED", "error", r"패키지 설치 실패", warn=True, title="패키지 설치 실패"),
    Rule("UPDATE.RESTART_FAILED", "error", r"다시 시작하지 못했습니다", warn=True,
         title="녹화 프로그램 재시작 실패"),
    # 작업 스케줄러·실행 정책
    Rule("TASKS.REGISTER_FAILED", "critical", r"자동 등록 실패|작업 등록이 거부", warn=True,
         title="작업 스케줄러 등록 실패"),
    Rule("TASKS.CHECK_FAILED", "error", r"작업 스케줄러를 확인할 수 없", warn=True,
         title="작업 스케줄러 확인 실패"),
    Rule("EXEC_POLICY.BLOCKED", "error", r"실행 정책.*(켜지 못|막혀)", warn=True,
         title="PowerShell 실행 정책"),
    # 송출
    Rule("STREAM.RECOVERY_FAILED", "error", r"복구 실패", logger="syncnote.stream", warn=True,
         title="송출 복구 실패"),
    Rule("STREAM.FAILED", "error", r".", logger="syncnote.stream", title="송출 오류"),
    # eClass 수집
    Rule("ECLASS.LOGIN_FAILED", "error", r"로그인.*실패|로그인하지 못", logger="syncnote.eclass", warn=True,
         title="eClass 로그인 실패"),
    Rule("ECLASS.FAILED", "error", r".", logger="syncnote.eclass", title="eClass 수집 오류"),
    # 설정
    Rule("CONFIG.REPAIRED", "warning", r"기본값으로 대신|이 줄은 빼고 읽었", warn=True,
         title="설정 오류를 고쳐 씀"),
    Rule("CONFIG.INVALID", "error", r"설정 오류", warn=True, title="설정 오류"),
)

_SEVERITY_BY_LEVEL = {logging.CRITICAL: "critical", logging.ERROR: "error", logging.WARNING: "warning"}


def classify(record: logging.LogRecord) -> tuple[str, str, str] | None:
    """(코드, severity, 제목). 잡지 않을 기록이면 None."""
    if record.name.startswith(LOGGER_NAME):
        return None                      # 자기 자신(올리기 실패 등)은 다시 잡지 않음(되먹임 방지)
    explicit = getattr(record, "incident", None)
    msg = record.getMessage()
    if explicit:
        rule = next((r for r in RULES if r.code == explicit), None)
        sev = rule.severity if rule else _SEVERITY_BY_LEVEL.get(record.levelno, "error")
        return str(explicit), sev, (rule.title if rule else str(explicit))
    for r in RULES:
        if r.logger and not record.name.startswith(r.logger):
            continue
        if record.levelno < logging.ERROR and not r.warn:
            continue
        if re.search(r.pattern, msg):
            return r.code, r.severity, r.title
    if record.levelno >= logging.ERROR:
        part = record.name.replace("syncnote.", "").replace(".", "_").upper() or "ROOT"
        return f"UNCLASSIFIED.{part}", _SEVERITY_BY_LEVEL.get(record.levelno, "error"), "분류되지 않은 오류"
    return None


# --- 가리기 ------------------------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"(?i)(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{10,}"), "<토큰>"),
    (re.compile(r"(?i)(authorization|token|password|passwd|secret|api[_-]?key)(\s*[:=]\s*)\S+"), r"\1\2<가림>"),
    (re.compile(r"(?i)[A-Z]:\\Users\\[^\\/\s\"']+"), r"C:\\Users\\<사용자>"),
    (re.compile(r"/(home|Users)/[^/\s\"']+"), r"/\1/<사용자>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<메일>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
    (re.compile(r"(?<![\w.])\d{7,}(?![\w.])"), "<번호>"),
    (re.compile(r"(이름|성명|학번|name)\s*[:=]\s*\S+", re.I), r"\1: <가림>"),
)


def redact(text: str) -> str:
    out = str(text)
    for pat, rep in _REDACTIONS:
        out = pat.sub(rep, out)
    return out


def normalize(message: str) -> str:
    """같은 종류의 오류가 같은 모양이 되게(숫자·경로·따옴표 안을 지움) — 묶을 때 씀."""
    m = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", message)
    m = re.sub(r"[A-Za-z]:\\[^\s]+|/[^\s]+", "<경로>", m)
    m = re.sub(r"\d+(\.\d+)?", "#", m)
    return m.strip()[:160]


# --- 잡기 --------------------------------------------------------------------------

def _git_version() -> str:
    try:
        root = Path(__file__).resolve().parents[2]
        cp = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=10,
                            creationflags=_NO_WINDOW)
        return cp.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class IncidentHandler(logging.Handler):
    """분류해서 대기열 파일에 한 줄씩. 절대 예외를 밖으로 내지 않음."""

    def __init__(self, queue_path: Path, machine: str, *, context_lines: int = 40) -> None:
        super().__init__(level=logging.INFO)          # INFO 는 문맥으로만 보관, 대기열은 WARNING 이상
        self.queue_path = Path(queue_path)
        self.machine = machine
        self.recent: deque[str] = deque(maxlen=max(5, int(context_lines)))
        self._lock = threading.Lock()
        self._fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        self.command = " ".join(sys.argv[1:4])
        self.version = ""

    def handle(self, record: logging.LogRecord) -> bool:     # INFO 도 문맥으로는 보관
        try:
            if not record.name.startswith(LOGGER_NAME):
                self.recent.append(self._fmt.format(record))
        except Exception:  # noqa: BLE001
            pass
        return super().handle(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING:
                return
            got = classify(record)
            if got is None:
                return
            code, severity, title = got
            exc = ""
            if record.exc_info:
                exc = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            if not self.version:
                self.version = _git_version()
            item = {
                "at": record.created, "machine": self.machine, "host": socket.gethostname(),
                "code": code, "severity": severity, "title": title,
                "logger": record.name, "level": record.levelname,
                "message": record.getMessage()[:2000], "exception": exc,
                "context": list(self.recent)[:-1], "command": self.command,
                "pid": os.getpid(), "version": self.version,
            }
            line = json.dumps(item, ensure_ascii=False)
            with self._lock:
                self.queue_path.parent.mkdir(parents=True, exist_ok=True)
                with self.queue_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # noqa: BLE001 — 잡다가 실패해도 원래 로그는 계속
            pass


# --- 올리기 ------------------------------------------------------------------------

class RestTransport:
    """GitHub REST(저장해 둔 토큰). release.ReleaseUploader 의 요청(재시도 포함)을 빌림."""

    def __init__(self, owner: str, repo: str, token: str) -> None:
        from .release import API, ReleaseUploader

        self.client = ReleaseUploader(owner, repo, token=token, retry_count=2)
        self.base = f"{API}/repos/{owner}/{repo}"
        self.name = "REST"

    def create(self, title: str, body: str, labels: list[str]) -> int:
        issue = self.client._request("POST", f"{self.base}/issues", timeout=30,
                                     json={"title": title, "body": body, "labels": labels}) or {}
        return int(issue["number"])

    def comment(self, number: int, body: str) -> None:
        self.client._request("POST", f"{self.base}/issues/{number}/comments", timeout=30, json={"body": body})

    def is_open(self, number: int) -> bool:
        issue = self.client._request("GET", f"{self.base}/issues/{number}", timeout=30) or {}
        return issue.get("state") == "open"


class GhTransport:
    """gh CLI(gh auth login 이 돼 있을 때)."""

    def __init__(self, owner: str, repo: str) -> None:
        self.repo = f"{owner}/{repo}"
        self.name = "gh"

    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        cmd = ["gh", "api", "-X", method, f"repos/{self.repo}/{path}"]
        cp = subprocess.run(cmd + (["--input", "-"] if payload is not None else []),
                            input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=60, creationflags=_NO_WINDOW)
        if cp.returncode != 0:
            raise RuntimeError((cp.stderr or cp.stdout).strip()[-300:])
        return json.loads(cp.stdout or "{}")

    def create(self, title: str, body: str, labels: list[str]) -> int:
        return int(self._api("POST", "issues", {"title": title, "body": body, "labels": labels})["number"])

    def comment(self, number: int, body: str) -> None:
        self._api("POST", f"issues/{number}/comments", {"body": body})

    def is_open(self, number: int) -> bool:
        return self._api("GET", f"issues/{number}").get("state") == "open"


def gh_ready() -> bool:
    try:
        cp = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=20, creationflags=_NO_WINDOW)
        return cp.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def make_transport(owner: str, repo: str):
    """토큰(REST) → gh → 없음(None)."""
    if not owner or not repo:
        return None
    try:
        from . import secrets

        token = secrets.get("github", required=False) or ""
    except Exception:  # noqa: BLE001
        token = ""
    if token:
        return RestTransport(owner, repo, token)
    if gh_ready():
        return GhTransport(owner, repo)
    return None


SEV_ICON = {"critical": "🔴", "error": "🟠", "warning": "🟡"}


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def issue_title(item: dict) -> str:
    return f"[{item['machine']}] {item['code']} — {item['title']}"


def issue_body(item: dict, count: int) -> str:
    ctx = "\n".join(redact(x) for x in item.get("context", [])[-40:])
    exc = redact(item.get("exception", ""))
    return "\n".join([
        f"{SEV_ICON.get(item['severity'], '')} **{item['title']}** (`{item['code']}`, {item['severity']})",
        "",
        "| 항목 | 값 |", "|---|---|",
        f"| PC | {item['machine']} |",
        f"| 처음 | {_when(item['at'])} |",
        f"| 이번 묶음 | {count}번 |",
        f"| 명령 | `{redact(item.get('command', ''))}` |",
        f"| 로거 | `{item['logger']}` ({item['level']}) |",
        f"| 버전 | `{item.get('version', '')}` |",
        "",
        "**메시지**", "```", redact(item["message"]), "```",
        *(["**예외**", "```", exc, "```"] if exc else []),
        *(["<details><summary>앞선 로그</summary>", "", "```", ctx, "```", "</details>"] if ctx else []),
        "",
        "_Sync Note 가 자동으로 올린 이슈입니다(src/syncnote/incidents.py). 같은 PC·같은 코드는 이 이슈에 "
        "댓글로 이어집니다. 해결되면 닫아 주세요 — 다시 나면 새로 엽니다._",
    ])


def comment_body(items: list[dict]) -> str:
    lines = [f"{SEV_ICON.get(items[-1]['severity'], '')} {len(items)}번 더 — "
             f"{_when(items[0]['at'])} ~ {_when(items[-1]['at'])}", ""]
    for it in items[-15:]:
        lines.append(f"- {_when(it['at'])} `{redact(it.get('command', ''))}` {redact(it['message'])[:300]}")
    last = items[-1]
    if last.get("exception"):
        lines += ["", "<details><summary>마지막 예외</summary>", "", "```",
                  redact(last["exception"])[-2500:], "```", "</details>"]
    if last.get("context"):
        lines += ["", "<details><summary>마지막 앞선 로그</summary>", "", "```",
                  "\n".join(redact(x) for x in last["context"][-25:]), "```", "</details>"]
    return "\n".join(lines)


@dataclass
class FlushResult:
    sent: int = 0
    created: list[int] = field(default_factory=list)
    commented: list[int] = field(default_factory=list)
    kept: int = 0
    error: str = ""


class Reporter:
    def __init__(self, state_dir: Path, transport_factory, *, labels=("incident",),
                 min_comment_minutes: float = 10.0, max_writes: int = 15) -> None:
        self.dir = Path(state_dir) / "incidents"
        self.queue = self.dir / "queue.jsonl"
        self.index = self.dir / "issues.json"
        self.transport_factory = transport_factory
        self.labels = list(labels)
        self.min_comment_sec = float(min_comment_minutes) * 60
        self.max_writes = int(max_writes)
        self._lock = threading.Lock()

    def _read_queue(self) -> list[dict]:
        try:
            lines = self.queue.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def _write_queue(self, items: list[dict]) -> None:
        tmp = self.queue.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
        tmp.replace(self.queue)

    def _index(self) -> dict:
        try:
            return json.loads(self.index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_index(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def flush(self, *, now: float | None = None) -> FlushResult:
        res = FlushResult()
        if not self._lock.acquire(blocking=False):
            return res
        try:
            return self._flush(res, now or time.time())
        except Exception as exc:  # noqa: BLE001 — 올리기 실패는 대기열에 남기고 다음에
            res.error = f"{type(exc).__name__}: {exc}"
            return res
        finally:
            self._lock.release()

    def _flush(self, res: FlushResult, now: float) -> FlushResult:
        # 다른 프로세스와 겹치지 않게(녹화 대기·감지·지킴이가 같은 대기열을 씀)
        self.dir.mkdir(parents=True, exist_ok=True)
        lockf = self.dir / "flush.lock"
        try:
            fd = os.open(str(lockf), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except FileExistsError:
            try:
                if time.time() - lockf.stat().st_mtime < 300:
                    return res
                lockf.unlink()
                return self._flush(res, now)
            except OSError:
                return res
        try:
            items = self._read_queue()
            if not items:
                return res
            transport = self.transport_factory()
            if transport is None:
                res.kept = len(items)
                res.error = "올릴 길이 없음(토큰·gh 로그인 없음 또는 레포 미설정)"
                return res
            groups: dict[str, list[dict]] = {}
            for it in items:
                groups.setdefault(f"{it['machine']}|{it['code']}", []).append(it)
            index = self._index()
            done_keys: set[str] = set()
            writes = 0
            for key, group in groups.items():
                if writes >= self.max_writes:
                    break
                entry = index.get(key) or {}
                number = entry.get("number")
                if number and not transport.is_open(int(number)):
                    number = None
                if number is None:
                    first = group[0]
                    labels = self.labels + [f"sev:{first['severity']}", f"pc:{first['machine']}"]
                    number = transport.create(issue_title(first), issue_body(first, len(group)), labels)
                    if len(group) > 1:
                        transport.comment(number, comment_body(group[1:]))
                    res.created.append(number)
                    index[key] = {"number": number, "last_comment": now}
                    writes += 1
                else:
                    if now - float(entry.get("last_comment", 0)) < self.min_comment_sec:
                        continue                                   # 모았다가 다음에
                    transport.comment(int(number), comment_body(group))
                    res.commented.append(int(number))
                    index[key] = {"number": number, "last_comment": now}
                    writes += 1
                done_keys.add(key)
                res.sent += len(group)
                self._save_index(index)
            left = [i for i in items if f"{i['machine']}|{i['code']}" not in done_keys]
            # 올리는 동안 새로 들어온 줄도 보존
            newer = self._read_queue()[len(items):]
            self._write_queue(left + newer)
            res.kept = len(left) + len(newer)
            return res
        finally:
            try:
                lockf.unlink()
            except OSError:
                pass


# --- 설치 ----------------------------------------------------------------------------

_installed: dict = {}


def install(cfg, layout, *, background: bool = True) -> Reporter | None:
    """syncnote 로거에 핸들러를 붙이고, 뒤에서 1분마다·끝날 때 올립니다. 한 번만."""
    if os.environ.get("SYNCNOTE_NO_INCIDENTS") or _installed:
        return _installed.get("reporter")
    if not bool(cfg.get("incidents.enabled", True)):
        return None
    repo_spec = str(cfg.get("incidents.repo", "") or "").strip()
    if "/" in repo_spec:
        owner, repo = repo_spec.split("/", 1)
    else:
        owner, repo = str(cfg.get("github.owner", "")), str(cfg.get("github.repo", ""))
    handler = IncidentHandler(layout.state / "incidents" / "queue.jsonl", cfg.machine_label,
                              context_lines=int(cfg.get("incidents.context_lines", 40)))
    logging.getLogger("syncnote").addHandler(handler)
    reporter = Reporter(layout.state, lambda: make_transport(owner, repo),
                        labels=list(cfg.get("incidents.labels", ["incident"]) or ["incident"]),
                        min_comment_minutes=float(cfg.get("incidents.min_comment_minutes", 10)),
                        max_writes=int(cfg.get("incidents.max_writes_per_flush", 15)))
    _installed.update(handler=handler, reporter=reporter)
    _install_hooks()
    if background:
        def loop():
            while True:
                time.sleep(60)
                reporter.flush()
        threading.Thread(target=loop, name="incidents-flush", daemon=True).start()
        atexit.register(_flush_at_exit, reporter)
    return reporter


def _flush_at_exit(reporter: Reporter) -> None:
    t = threading.Thread(target=reporter.flush, daemon=True)
    t.start()
    t.join(20)                          # 끝날 때 오래 붙잡지 않음 — 못 올린 것은 다음 실행이 올림


def _install_hooks() -> None:
    """처리되지 않은 예외(메인·스레드)도 잡음."""
    logger = logging.getLogger("syncnote.uncaught")
    old_hook = sys.excepthook

    def hook(et, ev, tb):
        if not issubclass(et, KeyboardInterrupt):
            logger.critical("처리되지 않은 예외: %s", ev, exc_info=(et, ev, tb))
        old_hook(et, ev, tb)

    sys.excepthook = hook
    old_thread = threading.excepthook

    def thook(args):
        if not issubclass(args.exc_type, (KeyboardInterrupt, SystemExit)):
            logger.critical("처리되지 않은 예외(스레드 %s): %s", getattr(args.thread, "name", "?"),
                            args.exc_value, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        old_thread(args)

    threading.excepthook = thook
