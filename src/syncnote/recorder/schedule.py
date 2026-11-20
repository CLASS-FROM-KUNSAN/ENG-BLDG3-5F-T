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
            slots.append(
                Slot(
                    name=str(entry.get("name", "수업")),
                    day=str(entry["day"]).lower(),
                    start=str(entry["start"]),
                    end=str(entry["end"]),
                    language=_as_language(entry.get("language")),
                )
            )
        except (KeyError, ValueError) as exc:
            log.error("시간표 항목을 건너뜁니다 (%s): %s", exc, entry)
    return slots


@dataclass
class Occurrence:
    slot: Slot
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


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


def describe(slots: list[Slot], skip_days: set[date] | None = None) -> str:
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
    nxt = next_occurrence(slots, skip_days=skip_days)
    if nxt:
        lines.append("")
        lines.append(f"다음 녹화: {nxt.start:%Y-%m-%d(%a) %H:%M} ~ {nxt.end:%H:%M}  [{nxt.slot.name}]")
    return "\n".join(lines)
