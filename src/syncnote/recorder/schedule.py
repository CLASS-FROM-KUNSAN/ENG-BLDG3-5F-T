"""시간표 기반 자동 녹화.

작업 스케줄러만으로도 '매주 월 09:00 실행' 은 가능합니다.
그럼 이 모듈이 왜 필요한가:
  - 과목이 7개면 작업을 7개 만들어야 하고, 시간표가 바뀔 때마다
    작업 스케줄러를 다시 건드려야 합니다.
  - 노트북 뚜껑을 닫고 다니면 놓친 작업이 엉뚱한 때 실행됩니다.
  - 종료 시각을 스케줄러가 관리하지 못합니다.
이 모듈을 쓰면 작업 스케줄러에는 '로그온 시 한 번' 만 걸어 두고,
시간표는 config.toml 에서 고치면 됩니다.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..logging_setup import get as get_logger

log = get_logger("recorder.schedule")

DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


@dataclass(frozen=True)
class Slot:
    name: str
    day: str
    start: str  # "HH:MM"
    end: str
    # 과목마다 언어가 다릅니다. 영어 수업을 ko 로 전사하면 소리 나는 대로
    # 한글을 뱉어 전사본이 통째로 쓸모없어집니다.
    language: str | None = None
    # 시간표 편집기가 함께 적는 칸. 녹화 파일 메타데이터와 과목 판정에 씀.
    course_id: str = ""
    room: str = ""
    professor: str = ""

    @property
    def weekday(self) -> int:
        return DAY_INDEX[self.day.lower()]

    def _at(self, on: date, hhmm: str) -> datetime:
        hour, minute = (int(x) for x in hhmm.split(":"))
        return datetime.combine(on, datetime.min.time()).replace(hour=hour, minute=minute)

    def occurrence(self, on: date) -> tuple[datetime, datetime]:
        start = self._at(on, self.start)
        end = self._at(on, self.end)
        if end <= start:  # 자정을 넘기는 수업
            end += timedelta(days=1)
        return start, end

    def __str__(self) -> str:
        suffix = f"  [{self.language}]" if self.language else ""
        return f"{DAY_KR[self.weekday]} {self.start}-{self.end}  {self.name}{suffix}"


def _as_language(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return "+".join(str(v) for v in value)
    return str(value)


def parse(entries: Iterable[dict]) -> list[Slot]:
    slots: list[Slot] = []
    for entry in entries or []:
        try:
            slot = Slot(
                name=str(entry.get("name", "수업")),
                day=str(entry["day"]).lower(),
                start=str(entry["start"]),
                end=str(entry["end"]),
                language=_as_language(entry.get("language")),
                course_id=str(entry.get("course_id", "") or ""),
                room=str(entry.get("room", "") or ""),
                professor=str(entry.get("professor", "") or ""),
            )
            # 여기서 걸러야 합니다. 그냥 두면 요일·시각이 깨진 행이 나중에
            # 일정 계산 중 KeyError/ValueError 로 대기 루프 전체를 죽입니다.
            if slot.day not in DAY_INDEX:
                raise ValueError(f"알 수 없는 요일 {slot.day!r}")
            slot.occurrence(date(2000, 1, 3))
            slots.append(slot)
        except (KeyError, ValueError, AttributeError) as exc:
            log.error("시간표 항목을 건너뜁니다 (%s): %s", exc, entry)
    return slots


@dataclass
class Occurrence:
    slot: Slot
    start: datetime
    end: datetime
    # 아래는 겹침 계획(next_window)이 채움. 비어 있으면 예전과 같은 뜻.
    video_from: datetime | None = None     # 화면을 켤 시각(진짜 수업 시작 또는 넘겨받은 시각)
    slots: tuple = ()                      # 이 녹화에 든 수업들(merge 면 둘 이상)
    label: str = ""
    # (Slot, 예정 수업 시작, 예정 수업 끝) — 파일 메타데이터에 남김
    classes: tuple = ()

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60

    @property
    def name(self) -> str:
        return self.label or self.slot.name


# ---------------------------------------------------------------------------
# 겹치는 수업 계획
# ---------------------------------------------------------------------------
# 녹화는 한 번에 하나뿐입니다. 하루치 수업을 실제 '녹화 창' 들로 바꾸고,
# 녹화되지 않는 수업 시간을 함께 돌려줍니다. 녹화기와 시간표 편집기 경고가
# 같은 계산을 써서, 편집기가 말한 대로 녹화됩니다.
#
#   여유 시간(pre/post roll)끼리만 겹침 → 정책과 무관하게 두 수업 사이 한가운데서
#       나눔. 수업 시간은 잃지 않음(바로 이어지는 수업이 앞 수업의 post-roll 때문에
#       앞부분을 잃던 것을 막음).
#   수업 시간이 겹침 → recording.overlap
#       first  앞 수업을 끝까지, 뒤 수업은 그 뒤부터(뒤 수업 앞부분을 잃음)
#       last   뒤 수업이 시작하면 넘김. 뒤 수업이 앞 수업 안에 쏙 들어가면 끝난 뒤
#              앞 수업을 이어서(앞 수업 가운데를 잃음)
#       merge  앞 수업 시작부터 뒤 수업 끝까지 한 파일(잃는 시간 없음, 이름에 둘 다) [기본]
OVERLAP_POLICIES = {"first": "앞 수업 우선", "last": "뒤 수업 우선", "merge": "하나로 이어서"}
# 기본: 겹치면 앞 수업 시작부터 뒤 수업 끝까지 한 번에(원테이크). 잃는 시간이 없고,
# 수업별 시작·끝은 파일 메타데이터(태그·챕터)에 남습니다(recorder.meta).
DEFAULT_POLICY = "merge"


@dataclass
class Window:
    slots: list
    start: datetime          # 녹화 시작(여유 시간 포함)
    end: datetime            # 녹화 끝
    cls_start: datetime      # 이 창이 맡은 수업 시간
    cls_end: datetime
    resumed: bool = False    # last 정책에서 끊겼다가 이어서 녹화하는 부분

    @property
    def slot(self) -> Slot:
        return self.slots[0]

    @property
    def label(self) -> str:
        return " + ".join(s.name for s in self.slots)


@dataclass
class Lost:
    slot: Slot
    start: datetime
    end: datetime
    because: str             # 그때 녹화 중인 수업

    @property
    def minutes(self) -> int:
        return round((self.end - self.start).total_seconds() / 60)


def plan_day(slots: list[Slot], day: date, pre_roll_min: float = 0.0,
             post_roll_min: float = 0.0, policy: str = DEFAULT_POLICY) -> tuple[list[Window], list[Lost]]:
    pre, post = timedelta(minutes=pre_roll_min), timedelta(minutes=post_roll_min)
    todays = []
    for s in slots:
        if s.weekday == day.weekday():
            cs, ce = s.occurrence(day)
            todays.append(Window([s], cs - pre, ce + post, cs, ce))
    todays.sort(key=lambda w: (w.cls_start, w.cls_end, w.slot.name))
    out: list[Window] = []
    lost: list[Lost] = []
    queue = list(todays)
    while queue:
        w = queue.pop(0)
        if not out or w.start >= out[-1].end:
            out.append(w)
            continue
        prev = out[-1]
        if prev.cls_end <= w.cls_start:
            # 여유 시간끼리만 겹침 → 두 수업 사이 한가운데서 나눔
            b = prev.cls_end + (w.cls_start - prev.cls_end) / 2
            prev.end = min(prev.end, b)
            w.start = max(w.start, b)
            out.append(w)
            continue
        if policy == "merge":
            prev.slots.extend(s for s in w.slots if s not in prev.slots)
            prev.end = max(prev.end, w.end)
            prev.cls_end = max(prev.cls_end, w.cls_end)
            continue
        if policy == "last":
            b = w.cls_start
            orig_end = prev.end
            lost.append(Lost(prev.slot, max(b, prev.cls_start), min(prev.cls_end, w.cls_end),
                             w.label))
            prev.end = b
            if prev.end <= prev.start:
                out.pop()
            w.start = max(b, out[-1].end) if out else min(w.start, b)
            if prev.cls_end > w.cls_end:
                # 뒤 수업이 앞 수업 안에 들어감 → 끝나면 앞 수업을 이어서
                w.end = min(w.end, w.cls_end)
                queue.insert(0, Window(list(prev.slots), w.cls_end, orig_end, w.cls_end,
                                       prev.cls_end, resumed=True))
            out.append(w)
            continue
        # first: 앞 수업은 수업 끝까지(post-roll 은 뒤 수업에 양보), 뒤 수업은 그 뒤부터
        b = prev.cls_end
        if w.cls_end <= b:
            # 뒤 수업이 앞 수업 안에 쏙 들어감 → 통째로 못 함. 앞 수업은 넘겨줄 상대가
            # 없으니 post-roll 을 그대로 둠.
            lost.append(Lost(w.slot, w.cls_start, w.cls_end, prev.label))
            continue
        prev.end = min(prev.end, max(b, w.start))
        lost.append(Lost(w.slot, w.cls_start, b, prev.label))
        w.start = b
        out.append(w)
    return out, lost


def next_window(slots: list[Slot], *, now: datetime | None = None,
                pre_roll_min: float = 0.0, post_roll_min: float = 0.0,
                horizon_days: int = 8, skip_days: set[date] | None = None,
                policy: str = DEFAULT_POLICY) -> Occurrence | None:
    """next_occurrence 와 같되, 겹치는 수업을 정책대로 나눈 실제 녹화 창 기준."""
    now = now or datetime.now()
    skip = skip_days or set()
    limit = horizon_days + (366 if skip else 0)
    # 자정을 넘기는 수업이 있으니 어제부터 봅니다.
    for offset in range(-1, limit):
        day = (now + timedelta(days=offset)).date()
        if day in skip:
            continue
        windows, _ = plan_day(slots, day, pre_roll_min, post_roll_min, policy)
        ahead = [w for w in windows if w.end > now]
        if ahead:
            w = min(ahead, key=lambda w: w.start)
            video = w.start if w.resumed else max(w.start, w.cls_start)
            classes = tuple((sl, *sl.occurrence(day)) for sl in w.slots)
            return Occurrence(w.slot, w.start, w.end, video_from=video,
                              slots=tuple(w.slots), label=w.label, classes=classes)
    return None


def overlap_report(slots: list[Slot], pre_roll_min: float = 0.0, post_roll_min: float = 0.0,
                   policy: str = DEFAULT_POLICY, today: date | None = None) -> list[tuple[int, Lost]]:
    """요일마다(다가오는 그 요일 기준) 녹화되지 않는 수업 시간. (요일 번호, Lost)."""
    today = today or date.today()
    out = []
    for wd in range(7):
        day = today + timedelta(days=(wd - today.weekday()) % 7)
        _, lost = plan_day(slots, day, pre_roll_min, post_roll_min, policy)
        out.extend((wd, x) for x in lost if x.end > x.start)
    return out


def describe_lost(wd: int, x: Lost, policy: str) -> str:
    return (f"{DAY_KR[wd]} {x.slot.name} {x.start:%H:%M}~{x.end:%H:%M} ({x.minutes}분) "
            f"녹화 안 됨 — {x.because} 녹화 중 · 정책: {OVERLAP_POLICIES.get(policy, policy)}")


def holidays(entries: Iterable) -> set[date]:
    """`[[recording.holidays]]` -> 날짜 집합. 날짜를 못 읽는 행은 건너뜁니다."""
    out: set[date] = set()
    for entry in entries or []:
        raw = entry.get("date") if isinstance(entry, dict) else entry
        try:
            if isinstance(raw, datetime):
                out.add(raw.date())
            elif isinstance(raw, date):
                out.add(raw)
            else:
                out.add(date.fromisoformat(str(raw).strip()))
        except (TypeError, ValueError):
            log.error("공휴일 항목을 건너뜁니다: %s", entry)
    return out


def next_occurrence(slots: list[Slot], *, now: datetime | None = None,
                    pre_roll_min: float = 0.0, post_roll_min: float = 0.0,
                    horizon_days: int = 8,
                    skip_days: set[date] | None = None) -> Occurrence | None:
    """지금 이후(또는 진행 중) 가장 가까운 수업을 찾습니다.

    skip_days(공휴일) 에 든 날의 수업은 건너뜁니다. 공휴일이 이어져도
    다음 수업을 찾도록 그만큼 더 멀리 봅니다.
    """
    now = now or datetime.now()
    best: Occurrence | None = None
    skip = skip_days or set()

    # 쉬는 날이 있으면 같은 요일 수업이 몇 주 연달아 빠질 수 있으므로
    # 찾을 때까지(최대 1년) 봅니다. 찾으면 아래 break 로 바로 끝납니다.
    limit = horizon_days + (366 if skip else 0)
    for offset in range(limit):
        day = (now + timedelta(days=offset)).date()
        if day in skip:
            continue
        for slot in slots:
            if slot.weekday != day.weekday():
                continue
            start, end = slot.occurrence(day)
            start -= timedelta(minutes=pre_roll_min)
            end += timedelta(minutes=post_roll_min)
            if end <= now:
                continue
            candidate = Occurrence(slot, start, end)
            if best is None or candidate.start < best.start:
                best = candidate
        if best is not None and best.start.date() <= day:
            break
    return best


def wait_until(when: datetime, *, stop_check=None, poll_sec: float = 20.0) -> bool:
    """when 까지 대기합니다. 중간에 중단되면 False.

    노트북이 절전에 들어갔다 깨어나면 time.sleep 은 건너뛴 시간을
    인식하지 못하므로, 짧게 자고 매번 현재 시각을 다시 읽습니다.
    """
    while True:
        now = datetime.now()
        if now >= when:
            return True
        if stop_check is not None and stop_check():
            return False
        remaining = (when - now).total_seconds()
        if remaining > 3600:
            log.info("다음 일정까지 %.1f시간 대기", remaining / 3600)
        time.sleep(min(poll_sec, max(1.0, remaining)))


def describe(slots: list[Slot], skip_days: set[date] | None = None, *,
             pre_roll_min: float = 0.0, post_roll_min: float = 0.0,
             policy: str = DEFAULT_POLICY) -> str:
    if not slots:
        return ("시간표가 비어 있습니다.\n"
                "config.toml 의 [[recording.timetable]] 블록을 채우거나 "
                "`python -m syncnote timetable edit` 로 편집하세요.")
    lines = ["등록된 시간표", "=" * 40]
    for slot in sorted(slots, key=lambda s: (s.weekday, s.start)):
        lines.append(f"  {slot}")
    upcoming = sorted(d for d in (skip_days or ()) if d >= date.today())
    if upcoming:
        lines.append("")
        lines.append("쉬는 날(공휴일): " + ", ".join(f"{d:%m-%d}" for d in upcoming[:8])
                     + (f" 외 {len(upcoming) - 8}일" if len(upcoming) > 8 else ""))
    lost = overlap_report(slots, pre_roll_min, post_roll_min, policy)
    if lost:
        lines.append("")
        lines.append(f"겹치는 수업 (recording.overlap = {policy})")
        for wd, x in lost:
            lines.append("  ! " + describe_lost(wd, x, policy))
    nxt = next_window(slots, skip_days=skip_days, pre_roll_min=pre_roll_min,
                      post_roll_min=post_roll_min, policy=policy)
    if nxt:
        lines.append("")
        lines.append(f"다음 녹화: {nxt.start:%Y-%m-%d(%a) %H:%M} ~ {nxt.end:%H:%M}  [{nxt.name}]")
    return "\n".join(lines)
