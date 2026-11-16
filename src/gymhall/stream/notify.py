"""송출 링크가 생길 때마다 GitHub 이슈로 올립니다.

언제 무엇을 하는지는 전부 [stream.issue] 로 정합니다.

  처음 링크가 생김          -> 새 이슈 (labels, assignees, mentions)
  링크가 바뀜(기기 이름·IP)  -> on_change: "new"(새 이슈) / "comment"(같은 이슈에 댓글) / "none"
                               close_previous = true 면 옛 이슈는 새 이슈 번호를 남기고 닫음
  같은 링크가 이미 열린 이슈에 -> dedupe = true 면 다시 만들지 않음 (token 을 고정했을 때)
  송출 종료                  -> comment_on_stop 댓글, close_on_stop 닫기

**링크에는 접속 토큰이 들어갑니다.** 이슈를 읽을 수 있는 사람은 tailnet 에
들어와 있기만 하면 들을 수 있다는 뜻입니다. 그래서 기본으로 private
저장소에만 올리고(require_private_repo), include_token = false 면 토큰을
빼고 올립니다(그러면 토큰은 따로 전해야 합니다).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..logging_setup import get as get_logger
from .tailnet import redact

log = get_logger("stream.notify")

ON_CHANGE = ("new", "comment", "none")

DEFAULT_TITLE = "[실시간 송출] {label} — {started}"
DEFAULT_BODY = """\
## 실시간 오디오 송출 링크

| 항목 | 값 |
|---|---|
| 듣기 | {link} |
| 재생 페이지 | {player} |
| 기기 | `{host}` ({machine}) |
| tailnet | `{tailnet}` |
| 노출 방식 | `{mode}` |
| 소리 | {source} · {codec} {bitrate}kbps |
| 시작 | {started} |
| 생성 이유 | {reason} |

- 같은 **Tailscale 망(tailnet)** 에 들어와 있는 기기에서만 열립니다.
- 브라우저, VLC, 휴대폰 Tailscale 앱 + 브라우저로 들을 수 있습니다.
{token_note}
> 이 이슈는 `gymhall stream start` 가 자동으로 만들었습니다.
"""


@dataclass
class IssueSettings:
    enabled: bool = True
    owner: str = ""
    repo: str = ""
    title: str = DEFAULT_TITLE
    body: str = ""
    labels: list[str] = field(default_factory=lambda: ["live-stream"])
    label_color: str = "d93f0b"
    assignees: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    include_token: bool = True
    on_change: str = "new"
    close_previous: bool = True
    comment_on_stop: bool = True
    close_on_stop: bool = True
    dedupe: bool = True
    require_private_repo: bool = True


@dataclass
class LinkInfo:
    link: str
    player: str = ""
    host: str = ""
    tailnet: str = ""
    mode: str = ""
    label: str = ""
    machine: str = ""
    source: str = ""
    codec: str = ""
    bitrate: int = 0
    started: str = ""
    token: str = ""


def link_key(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()[:16]


def _shown(url: str, s: IssueSettings) -> str:
    if not url:
        return "(없음)"
    return url if s.include_token else redact(url)


def render(template: str, info: LinkInfo, s: IssueSettings, reason: str) -> str:
    """{link} {player} {host} {tailnet} {mode} {label} {machine} {source} {codec}
    {bitrate} {started} {reason} {token_note} 를 채웁니다(순수 함수).

    모르는 {이름} 은 그대로 둡니다 — 사용자가 적은 템플릿의 오타 하나로
    송출 전체가 멈추면 안 됩니다.
    """
    if info.token and not s.include_token:
        token_note = "- 접속 토큰은 이 이슈에 적지 않았습니다. 송출한 사람에게 따로 받으세요.\n"
    elif info.token:
        token_note = "- 링크의 `?t=` 가 접속 토큰입니다. 이 이슈 밖으로 퍼뜨리지 마세요.\n"
    else:
        token_note = ""
    values = {
        "link": _shown(info.link, s), "player": _shown(info.player, s),
        "host": info.host, "tailnet": info.tailnet, "mode": info.mode, "label": info.label,
        "machine": info.machine, "source": info.source, "codec": info.codec,
        "bitrate": str(info.bitrate), "started": info.started, "reason": reason,
        "token_note": token_note,
    }

    class _Keep(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    return template.format_map(_Keep(values))


class IssueNotifier:
    """GitHub REST 로 이슈를 열고 닫습니다. HTTP 는 release.ReleaseUploader 를 빌립니다."""

    def __init__(self, settings: IssueSettings, *, client=None, store=None,
                 dry_run: bool = False) -> None:
        self.s = settings
        self.client = client      # ReleaseUploader (owner/repo/_request)
        self.store = store        # state.Store (열린 이슈 기억)
        self.dry_run = dry_run
        self.current: int | None = None
        self.current_link: str = ""
        self._repo_ok: bool | None = None
        self._labels_ok = False

    # -- 준비 ---------------------------------------------------------
    @property
    def _base(self) -> str:
        from ..release import API

        return f"{API}/repos/{self.client.owner}/{self.client.repo}"

    def _req(self, method: str, url: str, **kw) -> Any:
        return self.client._request(method, url, timeout=30, **kw)

    def repo_allowed(self) -> bool:
        if self._repo_ok is not None:
            return self._repo_ok
        if self.dry_run or not self.s.require_private_repo:
            self._repo_ok = True
            return True
        info = self._req("GET", self._base) or {}
        if not info:
            log.error("이슈를 올릴 저장소 %s/%s 에 닿지 못했습니다(없거나 토큰 권한 부족).",
                      self.client.owner, self.client.repo)
            self._repo_ok = False
        elif not info.get("private", False):
            log.error("%s 는 public 저장소입니다. 송출 링크를 올리지 않습니다 "
                      "(require_private_repo = false 로 바꾸면 올립니다).", info.get("full_name"))
            self._repo_ok = False
        elif not info.get("has_issues", True):
            log.error("%s 는 이슈가 꺼져 있습니다(Settings > Features > Issues).", info.get("full_name"))
            self._repo_ok = False
        else:
            self._repo_ok = True
        return self._repo_ok

    def _ensure_labels(self) -> None:
        if self._labels_ok or self.dry_run:
            return
        for name in self.s.labels:
            from urllib.parse import quote

            if self._req("GET", f"{self._base}/labels/{quote(name, safe='')}") is None:
                try:
                    self._req("POST", f"{self._base}/labels",
                              json={"name": name, "color": self.s.label_color.lstrip("#")})
                except Exception as exc:  # 라벨 없이도 이슈는 올립니다
                    log.warning("라벨 '%s' 를 만들지 못했습니다: %s", name, exc)
        self._labels_ok = True

    # -- 상태 기억 -----------------------------------------------------
    def _remembered(self) -> dict:
        if self.store is None:
            return {}
        return dict(self.store.get_meta("stream_issues", {}) or {})

    def _remember(self, link: str, number: int | None) -> None:
        if self.store is None or self.dry_run:
            return
        data = self._remembered()
        if number is None:
            data.pop(link_key(link), None)
        else:
            data[link_key(link)] = number
        self.store.set_meta("stream_issues", data)
        self.store.save()

    def _still_open(self, number: int) -> bool:
        if self.dry_run:
            return False
        issue = self._req("GET", f"{self._base}/issues/{number}") or {}
        return issue.get("state") == "open"

    # -- 동작 ---------------------------------------------------------
    def _create(self, info: LinkInfo, reason: str) -> int | None:
        title = render(self.s.title or DEFAULT_TITLE, info, self.s, reason)
        body = render(self.s.body or DEFAULT_BODY, info, self.s, reason)
        if self.s.mentions:
            body = " ".join(self.s.mentions) + "\n\n" + body
        payload: dict[str, Any] = {"title": title, "body": body}
        if self.s.labels:
            payload["labels"] = list(self.s.labels)
        if self.s.assignees:
            payload["assignees"] = list(self.s.assignees)
        if self.dry_run:
            print(f"\n[dry-run] 이슈를 만들 것입니다: {title}\n{body}")
            # 0 = '가짜 이슈'. 이후 댓글·닫기 흐름도 dry-run 으로 보이게 합니다.
            return 0
        self._ensure_labels()
        issue = self._req("POST", f"{self._base}/issues", json=payload) or {}
        number = issue.get("number")
        log.info("송출 링크 이슈를 올렸습니다: #%s %s", number, issue.get("html_url", ""))
        return number

    def _comment(self, number: int, text: str) -> None:
        if self.dry_run:
            print(f"\n[dry-run] #{number} 에 댓글: {text}")
            return
        self._req("POST", f"{self._base}/issues/{number}/comments", json={"body": text})

    def _close(self, number: int) -> None:
        if self.dry_run:
            print(f"\n[dry-run] #{number} 닫기")
            return
        self._req("PATCH", f"{self._base}/issues/{number}",
                  json={"state": "closed", "state_reason": "completed"})

    def link_created(self, info: LinkInfo, *, reason: str) -> int | None:
        """새 링크가 생겼습니다. 올린(또는 이어 쓴) 이슈 번호를 돌려줍니다."""
        if not self.s.enabled:
            return None
        if info.link == self.current_link:
            return self.current
        try:
            if not self.repo_allowed():
                return None
            # 같은 링크로 이미 열린 이슈가 있으면(토큰 고정 + 같은 기기) 이어 씁니다.
            if self.s.dedupe:
                old = self._remembered().get(link_key(info.link))
                if old and self._still_open(int(old)):
                    log.info("같은 링크의 이슈 #%s 가 아직 열려 있어 새로 만들지 않습니다.", old)
                    self.current, self.current_link = int(old), info.link
                    return self.current

            previous = self.current
            if previous is not None and reason == "change":
                if self.s.on_change == "none":
                    log.info("링크가 바뀌었지만 on_change = none 이라 이슈를 건드리지 않습니다.")
                    self.current_link = info.link
                    return previous
                if self.s.on_change == "comment":
                    self._comment(previous, render(
                        "링크가 바뀌었습니다 ({reason}).\n\n- 듣기: {link}\n- 재생 페이지: {player}",
                        info, self.s, "기기 주소 변경"))
                    self.current_link = info.link
                    self._remember(info.link, previous)
                    return previous

            number = self._create(info, reason="처음 송출" if reason == "start" else "기기 주소 변경")
            if previous is not None and number is not None and self.s.close_previous:
                self._comment(previous, f"링크가 바뀌어 #{number} 로 옮겼습니다.")
                self._close(previous)
            self.current, self.current_link = number, info.link
            if number is not None:
                self._remember(info.link, number)
            return number
        except Exception as exc:
            # 이슈를 못 올렸다고 송출을 멈추지는 않습니다.
            log.error("송출 링크 이슈를 올리지 못했습니다: %s", exc)
            return None

    def stopped(self, *, reason: str, minutes: float, listeners: int) -> None:
        if not self.s.enabled or self.current is None:
            return
        try:
            if self.s.comment_on_stop:
                self._comment(self.current,
                              f"송출을 마쳤습니다 — {reason}. "
                              f"{minutes:.0f}분, 누적 청취 접속 {listeners}회. "
                              f"({datetime.now():%Y-%m-%d %H:%M})")
            if self.s.close_on_stop:
                self._close(self.current)
                self._remember(self.current_link, None)
        except Exception as exc:
            log.error("송출 종료를 이슈에 남기지 못했습니다: %s", exc)
