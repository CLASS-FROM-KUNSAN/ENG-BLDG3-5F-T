"""녹화 파일 안에 넣는 메타데이터(태그 + 챕터).

모든 녹화 파일(mkv/mp4)에 다음을 남깁니다. 파일만 따로 떨어져 돌아다녀도
무슨 수업을 언제 어떻게 녹화했는지 알 수 있게 하려는 것입니다.

태그(파일 전체)
  TITLE                       "자료구조 + 영어2 2026-11-23 08:58~11:03"
  DATE_RECORDED               이 파일(세그먼트)의 실제 시작 시각(ISO, 시간대 포함)
  COMMENT                     사람이 읽는 요약(수업별 시간·강의실·겹침 정책)
  SYNCNOTE_CLASSES            수업별 상세 JSON — 이름·요일·예정 시작/종료·과목코드·
                              과목/레포·강의실·교수·전사 언어
  SYNCNOTE_TRIGGER            schedule / manual / watch
  SYNCNOTE_OVERLAP_POLICY     merge / first / last (겹칠 때만 뜻이 있음)
  SYNCNOTE_WINDOW_START/END   이 녹화 전체의 계획 시작·끝(여유 시간 포함)
  SYNCNOTE_SEGMENT            세그먼트 번호, SYNCNOTE_SEGMENT_START/END 실제 시각
  SYNCNOTE_PRE_ROLL_MIN / POST_ROLL_MIN, SYNCNOTE_MACHINE, SYNCNOTE_VERSION

챕터(파일 안 구간)
  "수업 전 여유" → "자료구조" → "자료구조 + 영어2 (겹침)" → "영어2" → "수업 후 여유"
  처럼 시간대마다 무슨 수업인지. 플레이어에서 수업 시작으로 바로 건너뛸 수 있습니다.
  세그먼트(30분 단위 파일)마다 그 파일 안의 상대 시각으로 잘라 넣습니다.

비밀번호·토큰·학번 같은 계정 정보는 넣지 않습니다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

DAY_KR = ("월", "화", "수", "목", "금", "토", "일")
POLICY_KR = {"first": "앞 수업 우선", "last": "뒤 수업 우선", "merge": "하나로 이어서"}


def _iso(t: datetime | None) -> str:
    if t is None:
        return ""
    return (t if t.tzinfo else t.astimezone()).isoformat(timespec="seconds")


@dataclass
class ClassMeta:
    name: str
    start: datetime            # 예정된 수업 시작(여유 시간 제외)
    end: datetime
    course_id: str = ""
    course_name: str = ""      # eClass 과목명(설정의 과목과 이어졌을 때)
    repo: str = ""             # 올라갈 레포(owner/repo)
    room: str = ""
    professor: str = ""
    language: str = ""

    def as_json(self) -> dict:
        d = asdict(self)
        d["day"] = DAY_KR[self.start.weekday()]
        d["start"], d["end"] = _iso(self.start), _iso(self.end)
        return {k: v for k, v in d.items() if v not in ("", None)}

    def line(self) -> str:
        bits = [f"{self.name} {self.start:%H:%M}~{self.end:%H:%M}"]
        if self.room:
            bits.append(self.room)
        if self.professor:
            bits.append(self.professor)
        if self.course_id:
            bits.append(f"과목코드 {self.course_id}")
        return " · ".join(bits)


@dataclass
class RecordingMeta:
    label: str
    trigger: str = "manual"                 # schedule / manual / watch
    classes: list[ClassMeta] = field(default_factory=list)
    policy: str = ""
    window_start: datetime | None = None
    window_end: datetime | None = None
    pre_roll_min: float = 0.0
    post_roll_min: float = 0.0
    machine: str = ""
    version: str = ""

    def to_state(self) -> dict:
        """JSON 으로 저장(소리만 녹음한 뒤 나중에 파일을 만들 때 그대로 되살림)."""
        def t(v):
            return v.isoformat() if isinstance(v, datetime) else v
        d = {k: t(v) for k, v in asdict(self).items() if k != "classes"}
        d["classes"] = [{k: t(v) for k, v in asdict(c).items()} for c in self.classes]
        return d

    @classmethod
    def from_state(cls, d: dict) -> RecordingMeta:
        def t(v):
            return datetime.fromisoformat(v) if isinstance(v, str) and v else None
        d = dict(d)
        classes = [ClassMeta(**{**c, "start": t(c["start"]), "end": t(c["end"])})
                   for c in d.pop("classes", [])]
        for k in ("window_start", "window_end"):
            d[k] = t(d.get(k))
        return cls(classes=classes, **d)

    @property
    def overlapping(self) -> bool:
        cs = sorted(self.classes, key=lambda c: c.start)
        return any(b.start < a.end for a, b in zip(cs, cs[1:], strict=False))

    def summary(self) -> str:
        lines = [c.line() for c in self.classes] or [f"{self.label} (시간표 밖 녹화)"]
        if self.overlapping:
            lines.append(f"겹치는 수업 — {POLICY_KR.get(self.policy, self.policy)}")
        lines.append({"schedule": "시간표 자동 녹화", "manual": "수동 녹화",
                      "watch": "프로그램 감지 녹화"}.get(self.trigger, self.trigger))
        return "\n".join(lines)

    def tags(self, seg_start: datetime, seg_end: datetime, index: int) -> dict[str, str]:
        ws = self.window_start or seg_start
        we = self.window_end or seg_end
        title = f"{self.label} {ws:%Y-%m-%d %H:%M}~{we:%H:%M}"
        out = {
            "TITLE": title,
            "DATE_RECORDED": _iso(seg_start),
            "COMMENT": self.summary(),
            "SYNCNOTE_TRIGGER": self.trigger,
            "SYNCNOTE_CLASSES": json.dumps([c.as_json() for c in self.classes],
                                           ensure_ascii=False, separators=(",", ":")),
            "SYNCNOTE_SEGMENT": str(index),
            "SYNCNOTE_SEGMENT_START": _iso(seg_start),
            "SYNCNOTE_SEGMENT_END": _iso(seg_end),
            "SYNCNOTE_WINDOW_START": _iso(self.window_start),
            "SYNCNOTE_WINDOW_END": _iso(self.window_end),
            "SYNCNOTE_PRE_ROLL_MIN": f"{self.pre_roll_min:g}",
            "SYNCNOTE_POST_ROLL_MIN": f"{self.post_roll_min:g}",
            "SYNCNOTE_OVERLAP_POLICY": self.policy,
            "SYNCNOTE_MACHINE": self.machine,
            "SYNCNOTE_VERSION": self.version,
        }
        return {k: v for k, v in out.items() if v != ""}

    def timeline(self) -> list[tuple[str, datetime, datetime]]:
        """(이름, 시작, 끝) — 시간대마다 무슨 수업인지. 수업 밖은 '여유'."""
        if not self.classes:
            return []
        ws = self.window_start or min(c.start for c in self.classes)
        we = self.window_end or max(c.end for c in self.classes)
        points = sorted({ws, we, *(c.start for c in self.classes), *(c.end for c in self.classes)})
        points = [p for p in points if ws <= p <= we]
        out: list[tuple[str, datetime, datetime]] = []
        first_start = min(c.start for c in self.classes)
        last_end = max(c.end for c in self.classes)
        for a, b in zip(points, points[1:], strict=False):
            if b <= a:
                continue
            active = [c.name for c in self.classes if c.start <= a and b <= c.end]
            if len(active) > 1:
                name = " + ".join(active) + " (겹침)"
            elif active:
                name = active[0]
            elif b <= first_start:
                name = "수업 전 여유"
            elif a >= last_end:
                name = "수업 후 여유"
            else:
                name = "수업 사이"
            if out and out[-1][0] == name and out[-1][2] == a:
                out[-1] = (name, out[-1][1], b)
            else:
                out.append((name, a, b))
        return out

    def chapters(self, file_start: float, file_end: float) -> list[tuple[str, int, int]]:
        """이 파일(epoch 초 구간) 안의 챕터: (이름, 시작 ms, 끝 ms) — 파일 기준 상대 시각."""
        out = []
        for name, a, b in self.timeline():
            s = max(a.timestamp(), file_start)
            e = min(b.timestamp(), file_end)
            if e - s >= 1.0:
                out.append((name, int((s - file_start) * 1000), int((e - file_start) * 1000)))
        return out


def _esc(value: str) -> str:
    """FFMETADATA 값 이스케이프(= ; # \\ 줄바꿈)."""
    out = []
    for ch in str(value):
        if ch in "=;#\\\n":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def ffmetadata(tags: dict[str, str], chapters: list[tuple[str, int, int]]) -> str:
    """ffmpeg 의 FFMETADATA1 파일 본문(태그 + 챕터)."""
    lines = [";FFMETADATA1"]
    for k, v in tags.items():
        lines.append(f"{k}={_esc(v)}")
    for title, start, end in chapters:
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={_esc(title)}"]
    return "\n".join(lines) + "\n"
