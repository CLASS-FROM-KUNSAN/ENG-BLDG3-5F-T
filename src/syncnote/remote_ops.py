"""원격 git 작업 실행(소유자가 GitHub 로 올린 서명된 계획을 받아 정규화한 git 명령만 실행).

전제(사용자 요청 — 영가설): 강의실 PC 와 닿는 길이 GitHub 이슈·PR·레포·Gist 뿐입니다. SSH 가 끊긴
PC 의 레포가 틀어지면(AutoUpdate 병합 충돌 등) 소유자가 고칠 길이 없습니다. 그래서 **레포 소유자만**
실행 명령·순서를 담은 JSON 계획을 Gist(또는 이슈 본문)로 올리면, 이 PC 가 받아 그대로 실행합니다.

**이것은 아무 명령이나 실행하는 원격 셸이 아닙니다.** git 문서(Pro Git)에서 고른, 정규화한 git 작업만
실행합니다(OPS 허용 목록). 아래 네 가지를 모두 통과해야 한 줄이라도 실행합니다.

  1. 서명(가장 강한 문): 계획은 미리 나눠 가진 비밀(secrets set remote_ops)로 HMAC-SHA256 서명돼
     있어야 합니다. 서명이 맞지 않으면 아무도 — 레포에 글을 쓸 수 있어도 — 실행하지 못합니다.
  2. 소유자 확인: 계획을 올린 계정이 이 레포의 admin(소유자) 권한이어야 하고, owner_login 을 못박으면
     그 계정이어야 합니다. Gist 는 gist.owner.login, 이슈는 작성자 로그인으로 확인합니다.
  3. 재생·만료 방지: 계획의 seq(점점 커지는 수)·id·expires_at. 지난 seq·쓴 id·만료된 것은 거부.
  4. 허용 목록 + 검증: op 는 OPS 에 있는 것만, 인자는 안전한 글자만(옵션 주입 차단, 경로는 레포 안).
     위험한 작업(reset --hard·clean -f -d 등)은 계획의 confirm_destructive + 설정 allow_destructive +
     녹화 중이 아닐 때만. clean -x/-X·prune --expire·reflog 삭제·filter-branch·임의 셸은 목록에 없음.

실행 뒤 결과(각 단계의 종료 코드·출력)를 그 이슈에 댓글로(가린 뒤) 올려 소유자가 보게 합니다.
무슨 일이 있어도 예외를 밖으로 던지지 않습니다(이것 때문에 녹화가 멈추면 안 됨).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_setup import get as get_logger

log = get_logger("remote_ops")

VERSION = 1
LABEL = "remote-ops"
DONE_LABEL = "remote-ops-done"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 인자 글자 제한(옵션 주입 '-...' 과 셸 글자 차단). 레퍼런스·브랜치·sha.
_REF = re.compile(r"^(?!-)[0-9A-Za-z._/@{}\-]{1,200}$")
_NAME = re.compile(r"^(?!-)[0-9A-Za-z._/\-]{1,120}$")
_REMOTE = re.compile(r"^(?!-)[0-9A-Za-z._\-]{1,60}$")


class RemoteOpsError(Exception):
    pass


def _ref(v: str) -> str:
    v = str(v)
    if not _REF.match(v) or ".." in v:
        raise RemoteOpsError(f"안전하지 않은 레퍼런스: {v!r}")
    return v


def _remote(v: str) -> str:
    if not _REMOTE.match(str(v)):
        raise RemoteOpsError(f"안전하지 않은 리모트 이름: {v!r}")
    return str(v)


def _name(v: str) -> str:
    if not _NAME.match(str(v)):
        raise RemoteOpsError(f"안전하지 않은 이름: {v!r}")
    return str(v)


def _path(v: str, repo: Path) -> str:
    """레포 안의 경로만. '--' 뒤에 붙여 pathspec 으로 씁니다."""
    p = str(v)
    if p.startswith("-") or "\0" in p:
        raise RemoteOpsError(f"안전하지 않은 경로: {v!r}")
    full = (repo / p).resolve()
    if repo.resolve() not in full.parents and full != repo.resolve():
        raise RemoteOpsError(f"레포 밖의 경로: {v!r}")
    return p


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# --- 허용 목록(정규화한 git 작업) -----------------------------------------------------
# 분류: read(언제나 안전) · sync(어긋나면 아무것도 안 바꾸고 실패) · safe(되돌리거나 백업됨) ·
# destructive(작업을 잃을 수 있음 — 설정·confirm·녹화 아님일 때만).
READ, SYNC, SAFE, DESTRUCTIVE = "read", "sync", "safe", "destructive"


def _build(op: str, step: dict, repo: Path) -> list[str]:
    """step → git 인자 목록(앞의 'git -C <repo>' 는 뺀 것). 검증 실패면 RemoteOpsError."""
    g = step.get
    if op == "status":
        return ["status", "--porcelain=v2", "--branch"]
    if op == "log":
        return ["log", "--oneline", "-n", str(_int(g("count"), 1, 200, 20)),
                *([_ref(g("ref"))] if g("ref") else [])]
    if op == "reflog":
        return ["reflog", "--date=iso", "-n", str(_int(g("count"), 1, 200, 30))]
    if op == "fsck":
        return ["fsck", "--full"]                       # 진단(읽기 전용)
    if op == "remote":
        return ["remote", "-v"]
    if op == "rev-parse":
        return ["rev-parse", _ref(g("ref", "HEAD"))]
    if op == "count-objects":
        return ["count-objects", "-v"]
    if op == "branch-list":
        return ["branch", "-vv", "--all"]
    if op == "show":
        return ["show", "--stat", "--no-patch", _ref(g("ref", "HEAD"))]
    if op == "stash-list":
        return ["stash", "list"]
    # --- sync ---
    if op == "fetch":
        return ["fetch", *(["--prune"] if g("prune") else []), _remote(g("remote", "origin")),
                *([_ref(g("branch"))] if g("branch") else [])]
    if op == "pull-ff":
        return ["pull", "--ff-only", _remote(g("remote", "origin")), _ref(g("branch", "main"))]
    if op == "merge-ff":
        return ["merge", "--ff-only", _ref(g("ref"))]
    if op == "switch":
        return ["switch", *(["--detach"] if g("detach") else []), _ref(g("ref"))]
    if op == "checkout-branch":
        return ["checkout", _ref(g("branch"))]
    # --- safe(되돌리거나 백업) ---
    if op == "merge-abort":
        return ["merge", "--abort"]
    if op == "revert":
        return ["revert", "--no-edit", _ref(g("ref"))]
    if op == "revert-abort":
        return ["revert", "--abort"]
    if op == "stash-push":
        msg = str(g("msg", "remote-ops backup"))[:100]
        if "\n" in msg or msg.startswith("-"):
            raise RemoteOpsError("안전하지 않은 stash 메시지")
        return ["stash", "push", "-u", "-m", msg]       # -u: untracked 포함. -x(ignored) 는 안 씀
    if op == "stash-apply":
        return ["stash", "apply", *([_name(g("stash"))] if g("stash") else [])]
    if op == "stash-pop":
        return ["stash", "pop", *([_name(g("stash"))] if g("stash") else [])]
    if op == "branch-create":                           # reflog/fsck 로 찾은 커밋 복구에 씀
        return ["branch", _name(g("name")), _ref(g("sha"))]
    # --- destructive(설정·confirm·녹화 아님일 때만) ---
    if op == "reset-hard":
        return ["reset", "--hard", _ref(g("ref"))]
    if op == "clean":                                   # -f -d 고정. -x/-X 는 절대 없음(.venv·config 보호)
        return ["clean", "-f", "-d", *(["-n"] if g("dry_run") else [])]
    if op == "checkout-file":
        return ["checkout", "--", _path(g("path"), repo)]
    if op == "branch-delete-force":
        return ["branch", "-D", _name(g("name"))]
    raise RemoteOpsError(f"허용하지 않는 작업: {op}")


OP_CLASS = {
    "status": READ, "log": READ, "reflog": READ, "fsck": READ, "remote": READ, "rev-parse": READ,
    "count-objects": READ, "branch-list": READ, "show": READ, "stash-list": READ,
    "fetch": SYNC, "pull-ff": SYNC, "merge-ff": SYNC, "switch": SYNC, "checkout-branch": SYNC,
    "merge-abort": SAFE, "revert": SAFE, "revert-abort": SAFE, "stash-push": SAFE,
    "stash-apply": SAFE, "stash-pop": SAFE, "branch-create": SAFE,
    "reset-hard": DESTRUCTIVE, "clean": DESTRUCTIVE, "checkout-file": DESTRUCTIVE,
    "branch-delete-force": DESTRUCTIVE,
}
ALLOWED_OPS = tuple(OP_CLASS)


# --- 서명 ---------------------------------------------------------------------------

def canonical(plan: dict) -> bytes:
    body = {k: v for k, v in plan.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(plan: dict, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(plan), hashlib.sha256).hexdigest()


def verify_signature(plan: dict, secret: str) -> bool:
    got = str(plan.get("sig", ""))
    if not got or not secret:
        return False
    return hmac.compare_digest(got, sign(plan, secret))


# --- 계획 검증 ----------------------------------------------------------------------

def _targets(plan: dict) -> list[str]:
    t = plan.get("target", "all")
    return [str(x) for x in t] if isinstance(t, list) else [str(t)]


def _parse_time(v) -> float | None:
    try:
        s = str(v).replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass
class State:
    path: Path
    last_seq: int = -1
    done_ids: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(Path(path), int(d.get("last_seq", -1)), list(d.get("done_ids", []))[-500:])
        except (OSError, ValueError):
            return cls(Path(path))

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                {"last_seq": self.last_seq, "done_ids": self.done_ids[-500:]},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            log.debug("remote-ops 상태 저장 실패: %s", exc)


def validate(plan: dict, *, machine: str, secret: str, state: State, repo: Path,
             now: float | None = None) -> None:
    """실행해도 되는지. 안 되면 RemoteOpsError(이유)."""
    now = now or time.time()
    if not isinstance(plan, dict) or plan.get("version") != VERSION:
        raise RemoteOpsError("계획 형식이 아니거나 버전이 다릅니다")
    if not secret:
        raise RemoteOpsError("서명 비밀이 없습니다(python -m syncnote secrets set remote_ops). 아무것도 실행하지 않습니다")
    if not verify_signature(plan, secret):
        raise RemoteOpsError("서명이 맞지 않습니다 — 소유자가 서명한 계획이 아닙니다")
    pid = str(plan.get("id", ""))
    if not pid:
        raise RemoteOpsError("계획 id 가 없습니다")
    if pid in state.done_ids:
        raise RemoteOpsError(f"이미 실행한 계획입니다(id={pid})")
    try:
        seq = int(plan.get("seq"))
    except (TypeError, ValueError):
        raise RemoteOpsError("seq 가 없습니다") from None
    if seq <= state.last_seq:
        raise RemoteOpsError(f"지난 계획입니다(seq {seq} ≤ 마지막 {state.last_seq}) — 재생 방지")
    exp = _parse_time(plan.get("expires_at"))
    if exp is None:
        raise RemoteOpsError("expires_at 이 없습니다")
    if now > exp:
        raise RemoteOpsError("계획이 만료됐습니다")
    if "all" not in _targets(plan) and machine not in _targets(plan):
        raise RemoteOpsError(f"이 PC('{machine}') 대상이 아닙니다")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RemoteOpsError("steps 가 없습니다")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in ALLOWED_OPS:
            raise RemoteOpsError(f"{i}번 단계: 허용하지 않는 작업 {step!r}")
        _build(step["op"], step, repo)                  # 인자 검증(버리는 결과)
        if OP_CLASS[step["op"]] == DESTRUCTIVE and not plan.get("confirm_destructive"):
            raise RemoteOpsError(f"{i}번 단계 '{step['op']}' 는 위험한 작업입니다. "
                                 "계획에 confirm_destructive=true 가 있어야 합니다")


# --- 실행 ---------------------------------------------------------------------------

@dataclass
class StepResult:
    op: str
    klass: str
    status: str                  # ok · failed · skipped
    returncode: int | None = None
    output: str = ""
    note: str = ""


@dataclass
class RunResult:
    ran: bool = False
    reason: str = ""             # 실행하지 않았으면 왜
    steps: list[StepResult] = field(default_factory=list)
    plan_id: str = ""
    seq: int = -1

    @property
    def ok(self) -> bool:
        return self.ran and all(s.status != "failed" for s in self.steps)


def _git(repo: Path, args: list[str], timeout: float = 300) -> tuple[int, str]:
    import os
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true", GIT_PAGER="cat",
               GCM_INTERACTIVE="never", LC_ALL="C")
    try:
        cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout, env=env,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    out = (cp.stdout + ("\n" + cp.stderr if cp.stderr else "")).strip()
    return cp.returncode, out


def execute(plan: dict, repo: Path, *, allow_destructive: bool, recording: bool,
            dry_run: bool = False) -> RunResult:
    """계획을 실행합니다. validate 를 통과한 계획만 넘기세요. 예외를 던지지 않음."""
    res = RunResult(plan_id=str(plan.get("id", "")), seq=int(plan.get("seq", -1)))
    stop_on_error = plan.get("stop_on_error", True)
    stopped = False
    for step in plan["steps"]:
        op = step["op"]
        klass = OP_CLASS[op]
        if stopped:
            res.steps.append(StepResult(op, klass, "skipped", note="앞 단계 실패로 건너뜀"))
            continue
        if klass == DESTRUCTIVE:
            if not allow_destructive:
                res.steps.append(StepResult(op, klass, "skipped",
                                            note="위험한 작업이 꺼져 있음([remote_ops] allow_destructive=false)"))
                continue
            if recording:
                res.steps.append(StepResult(op, klass, "skipped", note="녹화 중이라 건너뜀"))
                continue
        try:
            argv = _build(op, step, repo)
        except RemoteOpsError as exc:
            res.steps.append(StepResult(op, klass, "failed", note=str(exc)))
            if stop_on_error:
                stopped = True
            continue
        if dry_run:
            res.steps.append(StepResult(op, klass, "ok", note="(dry-run) git " + " ".join(argv)))
            continue
        rc, out = _git(repo, argv)
        res.steps.append(StepResult(op, klass, "ok" if rc == 0 else "failed", rc, out[:4000]))
        if rc != 0 and stop_on_error:
            stopped = True
    res.ran = True
    return res


# --- 결과 보고(이슈 댓글) ------------------------------------------------------------

def _redact(text: str) -> str:
    try:
        from .incidents import redact
        return redact(text)
    except Exception:  # noqa: BLE001
        return text


_ICON = {"ok": "✅", "failed": "❌", "skipped": "⏭️"}


def report_text(plan: dict, res: RunResult, machine: str) -> str:
    lines = [f"### remote-ops 실행 결과 — `{machine}`", "",
             f"- 계획 `{res.plan_id}` seq {res.seq}",
             f"- 사유: {_redact(str(plan.get('reason', '') or '-'))}",
             f"- 종합: {'성공' if res.ok else '일부 실패'}", "",
             "| 단계 | 분류 | 결과 | 코드 |", "|---|---|---|---|"]
    for s in res.steps:
        lines.append(f"| `{s.op}` | {s.klass} | {_ICON.get(s.status,'')} {s.status} "
                     f"| {'' if s.returncode is None else s.returncode} |")
    for s in res.steps:
        body = s.note or s.output
        if body:
            lines += ["", f"<details><summary>{s.op}</summary>", "", "```",
                      _redact(body)[:3000], "```", "</details>"]
    lines += ["", "_Sync Note 가 자동으로 올린 실행 결과입니다(src/syncnote/remote_ops.py)._"]
    return "\n".join(lines)


# --- GitHub 에서 계획 받아오기 --------------------------------------------------------

def make_transport(owner: str, repo: str):
    """incidents 와 같은 2중(토큰 REST → gh)."""
    from . import incidents
    return incidents.make_transport(owner, repo)


@dataclass
class Candidate:
    issue: int
    author: str
    source: str                  # 'inline' | 'gist'
    plan: dict


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_GIST = re.compile(r"https://gist\.github\.com/(?:[\w-]+/)?([0-9a-f]{20,})")


class GitHubSource:
    """remote-ops 라벨이 붙은 열린 이슈에서 소유자(admin)가 올린 계획을 읽습니다."""

    def __init__(self, transport, *, owner_login: str = "", require_admin: bool = True) -> None:
        self.t = transport
        self.owner_login = owner_login
        self.require_admin = require_admin
        self._perm: dict[str, bool] = {}

    def _is_admin(self, login: str) -> bool:
        if not login:
            return False
        if self.owner_login and login.lower() != self.owner_login.lower():
            return False
        if not self.require_admin:
            return True
        if login not in self._perm:
            info = self.t.get_json(f"/repos/{self.t.repo_full}/collaborators/{login}/permission") or {}
            self._perm[login] = (info.get("permission") == "admin"
                                 or (info.get("user", {}).get("permissions", {}) or {}).get("admin") is True)
        return self._perm[login]

    def _gist_plan(self, gist_id: str, expect_login: str) -> dict | None:
        gist = self.t.get_json(f"/gists/{gist_id}") or {}
        owner = (gist.get("owner") or {}).get("login", "")
        if not owner or owner.lower() != expect_login.lower():
            log.warning("Gist 소유자(%s)가 계획을 올린 계정(%s)과 달라 무시합니다.", owner, expect_login)
            return None
        for f in (gist.get("files") or {}).values():
            name = str(f.get("filename", ""))
            if name.endswith((".json", ".txt")) or (f.get("content", "").strip().startswith("{")):
                try:
                    return json.loads(f.get("content") or "")
                except ValueError:
                    continue
        return None

    def poll(self) -> list[Candidate]:
        issues = self.t.get_json(
            f"/repos/{self.t.repo_full}/issues?state=open&labels={LABEL}&per_page=20") or []
        out: list[Candidate] = []
        for it in issues:
            if "pull_request" in it:
                continue
            author = (it.get("user") or {}).get("login", "")
            if not self._is_admin(author):
                log.warning("remote-ops 이슈 #%s 작성자 '%s' 가 admin 이 아니라 무시합니다.",
                            it.get("number"), author)
                continue
            body = it.get("body") or ""
            gist = _GIST.search(body)
            plan = None
            source = "inline"
            if gist:
                plan, source = self._gist_plan(gist.group(1), author), "gist"
            if plan is None:
                m = _JSON_BLOCK.search(body)
                if m:
                    try:
                        plan = json.loads(m.group(1))
                    except ValueError:
                        plan = None
            if plan is not None:
                out.append(Candidate(int(it["number"]), author, source, plan))
        return out

    def report(self, issue: int, text: str) -> None:
        self.t.post(f"/repos/{self.t.repo_full}/issues/{issue}/comments", {"body": text})

    def resolve(self, issue: int, *, ok: bool) -> None:
        try:
            self.t.post(f"/repos/{self.t.repo_full}/issues/{issue}/labels", {"labels": [DONE_LABEL]})
        except Exception:  # noqa: BLE001
            pass
        self.t.patch(f"/repos/{self.t.repo_full}/issues/{issue}",
                     {"state": "closed", "state_reason": "completed" if ok else "not_planned"})


# --- 한 번 돌기 ----------------------------------------------------------------------

@dataclass
class PollOutcome:
    checked: int = 0
    ran: int = 0
    rejected: list[str] = field(default_factory=list)
    error: str = ""


def poll_once(cfg, layout, *, dry_run: bool = False, source=None, secret: Any = None,
              recording=None, now: float | None = None) -> PollOutcome:
    """GitHub 에서 계획을 받아 검증·실행·보고. 예외를 던지지 않음."""
    out = PollOutcome()
    try:
        if not bool(cfg.get("remote_ops.enabled", False)):
            out.error = "꺼져 있음([remote_ops] enabled=false)"
            return out
        from . import secrets as _secrets
        secret = secret if secret is not None else (_secrets.get("remote_ops", required=False) or "")
        machine = cfg.machine_label
        state = State.load(layout.state / "remote_ops.json")
        repo = Path(__file__).resolve().parents[2]
        allow_dest = bool(cfg.get("remote_ops.allow_destructive", True))
        if recording is None:
            from .updater import recording_now
            recording = bool(recording_now(layout.state))
        if source is None:
            spec = str(cfg.get("remote_ops.repo", "") or "").strip()
            owner, name = (spec.split("/", 1) if "/" in spec
                           else (str(cfg.get("github.owner", "")), str(cfg.get("github.repo", ""))))
            transport = make_transport(owner, name)
            if transport is None:
                out.error = "GitHub 에 닿을 길이 없음(토큰·gh 로그인·레포 설정 확인)"
                return out
            source = GitHubSource(transport, owner_login=str(cfg.get("remote_ops.owner_login", "")),
                                  require_admin=bool(cfg.get("remote_ops.require_admin", True)))
        for cand in source.poll():
            out.checked += 1
            try:
                validate(cand.plan, machine=machine, secret=secret, state=state, repo=repo, now=now)
            except RemoteOpsError as exc:
                msg = f"#{cand.issue}: 거부 — {exc}"
                out.rejected.append(msg)
                log.warning("remote-ops %s", msg)
                # 재생·만료·대상 아님은 이 PC 가 처리할 게 아니므로 그냥 둡니다(다른 PC 가 쓸 수 있음).
                # 서명·형식 오류는 올린 사람이 알도록 댓글만 남기고 닫지 않습니다.
                if "서명" in str(exc) or "형식" in str(exc) or "허용하지 않는" in str(exc):
                    try:
                        source.report(cand.issue, f"❌ remote-ops 거부({machine}): {exc}")
                    except Exception:  # noqa: BLE001
                        pass
                continue
            log.warning("remote-ops 계획 실행: #%s seq %s (%s)", cand.issue, cand.plan.get("seq"),
                        _redact(str(cand.plan.get("reason", ""))))
            res = execute(cand.plan, repo, allow_destructive=allow_dest, recording=recording,
                          dry_run=dry_run)
            if not dry_run:
                state.last_seq = max(state.last_seq, res.seq)
                state.done_ids.append(res.plan_id)
                state.save()
            try:
                source.report(cand.issue, report_text(cand.plan, res, machine))
                if not dry_run and "all" not in _targets(cand.plan):
                    source.resolve(cand.issue, ok=res.ok)     # 단일 PC 대상이면 닫음
            except Exception as exc:  # noqa: BLE001
                log.error("remote-ops 결과 보고 실패: %s", exc)
            out.ran += 1
        return out
    except Exception as exc:  # noqa: BLE001 — 폴링 실패로 아무것도 멈추지 않음
        log.exception("remote-ops 폴링 중 오류")
        out.error = f"{type(exc).__name__}: {exc}"
        return out


# --- 소유자 쪽 도우미(계획 만들기·서명) ------------------------------------------------

def new_plan(*, target: str = "all", reason: str = "", seq: int | None = None,
             ttl_hours: float = 24.0, steps: list[dict] | None = None,
             confirm_destructive: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    return {
        "version": VERSION, "id": __import__("uuid").uuid4().hex,
        "seq": int(seq if seq is not None else now.timestamp()),
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": target, "reason": reason, "stop_on_error": True,
        "confirm_destructive": bool(confirm_destructive),
        "steps": steps or [{"op": "status"}],
    }


def sign_plan(plan: dict, secret: str) -> dict:
    plan = {k: v for k, v in plan.items() if k != "sig"}
    plan["sig"] = sign(plan, secret)
    return plan
