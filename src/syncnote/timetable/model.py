"""시간표 모델과 저장.

저장 위치는 **config.toml 의 `[[recording.timetable]]` / `[[recording.holidays]]`**
입니다. 녹화(`record schedule`)·송출(`stream schedule`)·과목 판정이 이미 이
표를 읽으므로, 편집기가 따로 파일을 두면 두 군데가 어긋납니다.

config.toml 은 사람이 주석을 달아 쓰는 파일이라 통째로 다시 쓰지 않습니다.
편집기는 표시줄(BEGIN/END) 사이 한 덩어리만 관리하고, 그 밖은 한 글자도
안 바꿉니다. 예외는 하나 — 손으로 적어 둔 `[[recording.timetable]]` 표가
있으면 같은 수업이 두 번 잡히지 않도록 그 표를 주석 처리합니다(지우지 않음).
쓰기 전에 결과를 다시 파싱해 의도한 값과 같은지 확인하고, 원본은 `.bak`
으로 남깁니다.

화면의 '수업' 하나는 여러 요일을 가질 수 있지만(Swift 앱과 같음), TOML 에는
요일마다 한 줄씩 들어갑니다. 읽을 때 같은 수업(이름·시간·속성이 모두
같은 줄)을 다시 하나로 묶습니다.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10
    import tomli as tomllib

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_KR = ("월", "화", "수", "목", "금", "토", "일")
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}

# Swift 앱(AddCourseView)과 같은 팔레트.
PALETTE = (
    "#4A90D9", "#E74C3C", "#2ECC71", "#F39C12",
    "#9B59B6", "#1ABC9C", "#E67E22", "#34495E",
    "#FF6B9D", "#00B4D8", "#06D6A0", "#FFB703",
)
DEFAULT_COLOR = PALETTE[0]

BEGIN = "# >>> syncnote 시간표 편집기 (이 아래부터 END 까지는 편집기가 다시 씁니다) >>>"
END = "# <<< syncnote 시간표 편집기 <<<"
_MANAGED_HEADER = re.compile(r"^\s*\[\[\s*recording\s*\.\s*(timetable|holidays)\s*\]\]")
_ANY_HEADER = re.compile(r"^\s*\[")
COMMENT_TAG = "# (시간표 편집기로 옮김) "

_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


class TimetableError(ValueError):
    """시간표를 읽거나 쓸 수 없습니다."""


# ---------------------------------------------------------------------------
# 시간
# ---------------------------------------------------------------------------
def to_minutes(hhmm: str) -> int:
    m = _HHMM.match(str(hhmm).strip())
    if not m:
        raise TimetableError(f"시간은 HH:MM 이어야 합니다: {hhmm!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise TimetableError(f"없는 시각입니다: {hhmm!r}")
    return h * 60 + mi


def from_minutes(total: int) -> str:
    total = max(0, min(int(total), 23 * 60 + 59))
    return f"{total // 60:02d}:{total % 60:02d}"


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
@dataclass
class Course:
    name: str
    days: list[str] = field(default_factory=list)   # "mon".."sun"
    start: str = "09:00"
    end: str = "10:30"
    professor: str = ""
    room: str = ""
    color: str = DEFAULT_COLOR
    language: str = ""        # 비우면 [transcribe] 의 language
    course_id: str = ""       # eClass 과목코드(있으면 과목 판정이 정확해짐)

    def __post_init__(self) -> None:
        self.days = sorted({str(d).lower() for d in self.days},
                           key=lambda d: DAY_INDEX.get(d, 99))

    @property
    def start_min(self) -> int:
        return to_minutes(self.start)

    @property
    def end_min(self) -> int:
        return to_minutes(self.end)

    @property
    def days_text(self) -> str:
        return ", ".join(DAY_KR[DAY_INDEX[d]] for d in self.days if d in DAY_INDEX)

    @property
    def time_text(self) -> str:
        return f"{self.start} ~ {self.end}"

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.name.strip():
            out.append("수업명이 비어 있습니다")
        if not self.days:
            out.append(f"{self.name or '수업'}: 요일을 하나 이상 고르세요")
        bad = [d for d in self.days if d not in DAY_INDEX]
        if bad:
            out.append(f"{self.name}: 알 수 없는 요일 {bad}")
        try:
            if self.end_min <= self.start_min:
                out.append(f"{self.name}: 종료({self.end})가 시작({self.start})보다 늦어야 합니다")
        except TimetableError as exc:
            out.append(f"{self.name}: {exc}")
        if not _COLOR.match(self.color or ""):
            out.append(f"{self.name}: 색상은 #RRGGBB 이어야 합니다 ({self.color!r})")
        return out

    def copy(self, **changes) -> Course:
        changes.setdefault("days", list(self.days))
        return replace(self, **changes)


@dataclass
class Holiday:
    day: date
    name: str = ""


@dataclass
class Timetable:
    courses: list[Course] = field(default_factory=list)
    holidays: list[Holiday] = field(default_factory=list)

    def copy(self) -> Timetable:
        return Timetable([c.copy() for c in self.courses],
                         [Holiday(h.day, h.name) for h in self.holidays])

    # -- 점검 ------------------------------------------------------------
    def problems(self) -> list[str]:
        out: list[str] = []
        for c in self.courses:
            out.extend(c.problems())
        seen: set[date] = set()
        for h in self.holidays:
            if h.day in seen:
                out.append(f"공휴일 {h.day} 가 두 번 있습니다")
            seen.add(h.day)
        return out

    def overlaps(self) -> list[tuple[str, Course, Course]]:
        """같은 요일에 시간이 겹치는 수업 쌍 (요일, a, b).

        녹화는 한 번에 하나라 겹치면 뒤 수업이 앞 수업 녹화에 먹힙니다.
        저장은 막지 않고 경고만 합니다(분반 등 일부러 겹칠 수 있음).
        """
        out = []
        for day in DAYS:
            todays = [c for c in self.courses if day in c.days and not c.problems()]
            todays.sort(key=lambda c: c.start_min)
            for i, a in enumerate(todays):
                for b in todays[i + 1:]:
                    if b.start_min < a.end_min:
                        out.append((day, a, b))
        return out

    # -- config.toml 행 ---------------------------------------------------
    def to_rows(self) -> list[dict]:
        """`[[recording.timetable]]` 행 목록. 요일 순, 같은 요일은 시작 순."""
        rows = []
        for c in self.courses:
            for d in c.days:
                row = {"name": c.name.strip(), "day": d, "start": c.start, "end": c.end}
                if c.language.strip():
                    row["language"] = c.language.strip()
                if c.course_id.strip():
                    row["course_id"] = c.course_id.strip()
                if c.room.strip():
                    row["room"] = c.room.strip()
                if c.professor.strip():
                    row["professor"] = c.professor.strip()
                if c.color and c.color.upper() != DEFAULT_COLOR:
                    row["color"] = c.color.upper()
                rows.append(row)
        rows.sort(key=lambda r: (DAY_INDEX[r["day"]], to_minutes(r["start"]), r["name"]))
        return rows

    def holiday_rows(self) -> list[dict]:
        rows = []
        for h in sorted(self.holidays, key=lambda h: h.day):
            row = {"date": h.day.isoformat()}
            if h.name.strip():
                row["name"] = h.name.strip()
            rows.append(row)
        return rows

    @classmethod
    def from_rows(cls, rows, holiday_rows=()) -> Timetable:
        groups: dict[tuple, Course] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            lang = row.get("language", "")
            if isinstance(lang, (list, tuple)):
                lang = "+".join(str(x) for x in lang)
            key = (
                str(row.get("name", "")).strip(), str(row.get("start", "")),
                str(row.get("end", "")), str(lang), str(row.get("course_id", "")),
                str(row.get("room", "")), str(row.get("professor", "")),
                str(row.get("color", DEFAULT_COLOR)).upper(),
            )
            course = groups.get(key)
            if course is None:
                course = Course(name=key[0], days=[], start=key[1], end=key[2],
                                language=key[3], course_id=key[4], room=key[5],
                                professor=key[6], color=key[7])
                groups[key] = course
            course.days = sorted(set(course.days) | {str(row.get("day", "")).lower()},
                                 key=lambda d: DAY_INDEX.get(d, 99))
        holidays = []
        for row in holiday_rows or []:
            if not isinstance(row, dict):
                continue
            try:
                holidays.append(Holiday(_as_date(row.get("date")), str(row.get("name", ""))))
            except (TypeError, ValueError):
                continue
        return cls(list(groups.values()), holidays)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------
def _q(value: str) -> str:
    """TOML 기본 문자열. JSON 의 이스케이프가 TOML 과 호환됩니다."""
    return json.dumps(str(value), ensure_ascii=False)


def render_block(tt: Timetable) -> str:
    lines = [BEGIN,
             "# 수업마다 요일 하나씩 한 표입니다. 고치려면 `python -m syncnote timetable edit`.",
             ""]
    for row in tt.to_rows():
        lines.append("[[recording.timetable]]")
        width = max(len(k) for k in row)
        for k, v in row.items():
            lines.append(f"{k.ljust(width)} = {_q(v)}")
        lines.append("")
    for row in tt.holiday_rows():
        lines.append("[[recording.holidays]]")
        for k, v in row.items():
            lines.append(f"{k.ljust(4)} = {_q(v)}")
        lines.append("")
    lines.append(END)
    return "\n".join(lines) + "\n"


def _strip_block(lines: list[str]) -> list[str]:
    out, inside = [], False
    for ln in lines:
        s = ln.strip()
        if s == BEGIN:
            inside = True
            continue
        if inside:
            if s == END:
                inside = False
            continue
        out.append(ln)
    if inside:
        raise TimetableError("config.toml 에 시간표 편집기 시작 표시만 있고 끝 표시가 없습니다. "
                             "직접 확인해 주세요.")
    return out


def _comment_out_manual_tables(lines: list[str]) -> tuple[list[str], int]:
    """표시줄 밖의 [[recording.timetable]] / [[recording.holidays]] 를 주석 처리."""
    out, inside, count = [], False, 0
    for ln in lines:
        s = ln.strip()
        if _MANAGED_HEADER.match(ln):
            inside = True
            count += 1
            out.append(COMMENT_TAG + ln)
            continue
        if inside and _ANY_HEADER.match(ln):
            inside = False
        if inside and s and not s.startswith("#"):
            out.append(COMMENT_TAG + ln)
        else:
            out.append(ln)
    return out, count


def apply_to_text(text: str, tt: Timetable) -> tuple[str, int]:
    """config.toml 본문에 시간표를 반영한 새 본문과, 주석 처리한 손 작성 표 수."""
    problems = tt.problems()
    if problems:
        raise TimetableError("저장할 수 없습니다:\n  - " + "\n  - ".join(problems))
    lines = _strip_block(text.splitlines())
    lines, commented = _comment_out_manual_tables(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    body = "\n".join(lines)
    new = (body + "\n\n" if body else "") + render_block(tt)

    # 바꿨다고 믿는 것과 바뀐 것은 다릅니다. 다시 읽어 대조합니다.
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        raise TimetableError(f"저장 결과가 올바른 TOML 이 아닙니다: {exc}") from exc
    rec = parsed.get("recording", {}) if isinstance(parsed.get("recording"), dict) else {}
    if rec.get("timetable", []) != tt.to_rows() or rec.get("holidays", []) != tt.holiday_rows():
        raise TimetableError(
            "저장 결과를 다시 읽었더니 시간표가 다릅니다. config.toml 의 다른 곳에 "
            "recording.timetable / recording.holidays 가 정의돼 있는지 확인하세요."
        )
    return new, commented


def load(path: str | Path) -> Timetable:
    p = Path(path)
    if not p.exists():
        return Timetable()
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise TimetableError(f"{p} 를 읽을 수 없습니다: {exc}") from exc
    rec = data.get("recording", {})
    if not isinstance(rec, dict):
        return Timetable()
    return Timetable.from_rows(rec.get("timetable", []), rec.get("holidays", []))


def save(path: str | Path, tt: Timetable) -> int:
    """config.toml 에 저장. 원본은 <이름>.bak. 반환: 주석 처리한 손 작성 표 수."""
    p = Path(path)
    raw = p.read_bytes() if p.exists() else b""
    crlf = b"\r\n" in raw
    text = raw.decode("utf-8-sig") if raw else ""
    new, commented = apply_to_text(text, tt)
    if crlf:
        new = new.replace("\n", "\r\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw:
        p.with_name(p.name + ".bak").write_bytes(raw)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(new.encode("utf-8"))
    tmp.replace(p)
    return commented


# ---------------------------------------------------------------------------
# Swift 앱(2026년 시간표) 백업 JSON 호환
# ---------------------------------------------------------------------------
# Swift 쪽 weekdays 는 1=월 … 5=금 이고 이름표가 5칸뿐이라 6·7 을 넣으면
# 그 앱이 죽습니다. 내보낼 때 주말은 빼고 경고합니다.
_SWIFT_MAX_DAY = 5


def to_swift_json(tt: Timetable) -> tuple[str, list[str]]:
    warnings: list[str] = []
    courses = []
    for c in tt.courses:
        nums = [DAY_INDEX[d] + 1 for d in c.days if d in DAY_INDEX]
        dropped = [n for n in nums if n > _SWIFT_MAX_DAY]
        if dropped:
            warnings.append(f"{c.name}: Swift 앱은 주말을 못 받아 토·일을 뺐습니다")
        nums = [n for n in nums if n <= _SWIFT_MAX_DAY]
        sh, sm = divmod(c.start_min, 60)
        eh, em = divmod(c.end_min, 60)
        courses.append({
            "name": c.name, "professor": c.professor, "room": c.room,
            "colorHex": c.color.upper(), "weekdays": ",".join(str(n) for n in nums),
            "startHour": str(sh), "startMinute": str(sm),
            "endHour": str(eh), "endMinute": str(em),
        })
    holidays = []
    for h in tt.holidays:
        # Swift 는 '그 날 0시(기기 시간대)' 의 epoch 초를 씁니다.
        ts = int(datetime.combine(h.day, datetime.min.time()).timestamp())
        holidays.append({"name": h.name, "date": str(ts)})
    root = {"courses": courses, "holidays": holidays}
    return json.dumps(root, ensure_ascii=False, indent=2), warnings


def from_swift_json(text: str) -> Timetable:
    try:
        root = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TimetableError(f"JSON 형식이 올바르지 않습니다: {exc}") from exc
    if not isinstance(root, dict):
        raise TimetableError("JSON 최상위가 객체가 아닙니다")

    def num(d, k, default):
        try:
            return int(str(d.get(k, default)).strip())
        except ValueError:
            return default

    courses = []
    for c in root.get("courses", []) or []:
        if not isinstance(c, dict) or not str(c.get("name", "")).strip():
            continue
        days = []
        for part in str(c.get("weekdays", "")).split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 7:
                days.append(DAYS[int(part) - 1])
        color = str(c.get("colorHex", DEFAULT_COLOR))
        courses.append(Course(
            name=str(c["name"]).strip(), days=days,
            start=from_minutes(num(c, "startHour", 9) * 60 + num(c, "startMinute", 0)),
            end=from_minutes(num(c, "endHour", 10) * 60 + num(c, "endMinute", 30)),
            professor=str(c.get("professor", "")), room=str(c.get("room", "")),
            color=color.upper() if _COLOR.match(color) else DEFAULT_COLOR,
        ))
    holidays = []
    for h in root.get("holidays", []) or []:
        if not isinstance(h, dict):
            continue
        try:
            ts = float(str(h.get("date", "")).strip())
        except ValueError:
            continue
        holidays.append(Holiday(datetime.fromtimestamp(ts).date(), str(h.get("name", ""))))
    return Timetable(courses, holidays)


# ---------------------------------------------------------------------------
# 공휴일 붙여넣기 (Swift PasteHolidayView 와 같은 역할)
# ---------------------------------------------------------------------------
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*(?:월|[/.\-])\s*(\d{1,2})\s*일?(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s*[년/.\-]\s*(\d{1,2})\s*(?:월|[/.\-])\s*(\d{1,2})\s*일?(?!\d)")
# 날짜 바로 뒤의 '(일)', '(월요일)' 같은 요일 표기.
_WEEKDAY_NOISE = re.compile(r"^\s*[\(\[（]\s*[월화수목금토일]\s*(?:요일)?\s*[\)\]）]")


def parse_holiday_text(text: str, year: int) -> list[Holiday]:
    """한 줄에 하나씩 '1/1 신정', '3월 1일(일) 삼일절', '2026-05-05 어린이날' 등.

    날짜가 안 보이는 줄은 건너뜁니다. 같은 날짜는 처음 것만.
    """
    out: list[Holiday] = []
    seen: set[date] = set()
    for line in str(text).splitlines():
        m = _YMD.search(line)
        try:
            if m:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                m = _MD.search(line)
                if not m:
                    continue
                d = date(int(year), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        after = _WEEKDAY_NOISE.sub("", line[m.end():], count=1)
        name = re.sub(r"\s+", " ", line[:m.start()] + " " + after).strip(" -:·,\t")
        if d not in seen:
            seen.add(d)
            out.append(Holiday(d, name))
    return out


# ---------------------------------------------------------------------------
# 공휴일 편집
# ---------------------------------------------------------------------------
def edit_holiday(tt: Timetable, old_day: date, new: Holiday) -> Timetable:
    """old_day 공휴일을 new 로 바꾼 사본. 날짜를 다른 공휴일과 겹치게 바꾸면 거부."""
    if not any(h.day == old_day for h in tt.holidays):
        raise TimetableError(f"{old_day} 공휴일이 없습니다")
    if new.day != old_day and any(h.day == new.day for h in tt.holidays):
        raise TimetableError(f"{new.day} 는 이미 공휴일로 등록돼 있습니다")
    out = tt.copy()
    out.holidays = [Holiday(new.day, new.name.strip()) if h.day == old_day else h
                    for h in out.holidays]
    return out


def remove_holidays(tt: Timetable, days) -> Timetable:
    drop = set(days)
    out = tt.copy()
    out.holidays = [h for h in out.holidays if h.day not in drop]
    return out


# ---------------------------------------------------------------------------
# 다가오는 일정 — 저장 결과 확인용
# ---------------------------------------------------------------------------
@dataclass
class Upcoming:
    start: datetime
    end: datetime
    name: str
    skipped_for: str | None = None     # 공휴일로 건너뛰면 그 공휴일 이름("" 이면 이름 없음)

    @property
    def skipped(self) -> bool:
        return self.skipped_for is not None


def saved_rows(path: str | Path) -> tuple[list, list]:
    """config.toml 을 다시 읽은 그대로의 (timetable 행, holidays 행).

    편집기 메모리가 아니라 **파일**을 읽어, 녹화기가 실제로 볼 내용을 보여 줍니다.
    """
    p = Path(path)
    if not p.exists():
        return [], []
    data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    rec = data.get("recording", {})
    if not isinstance(rec, dict):
        return [], []
    return list(rec.get("timetable", []) or []), list(rec.get("holidays", []) or [])


def fingerprint(path: str | Path) -> str | None:
    """파일 내용 지문(sha256). 없으면 None. 수정 시각은 안 씁니다 — 초 단위라
    같은 초에 두 번 저장하면 못 알아보고, 내용이 같아도 바뀐 걸로 보입니다."""
    p = Path(path)
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def canonical(rows, holiday_rows) -> tuple:
    """시간표 부분만의 비교용 형태. 손으로 쓴 표·편집기 블록·줄 순서가 달라도
    뜻이 같으면 같게 나옵니다. 깨진 행이 있으면 원문 그대로 비교합니다."""
    try:
        tt = Timetable.from_rows(rows, holiday_rows)
        return (tuple(json.dumps(r, sort_keys=True, ensure_ascii=False) for r in tt.to_rows()),
                tuple(json.dumps(r, sort_keys=True, ensure_ascii=False)
                      for r in tt.holiday_rows()))
    except (TimetableError, KeyError, ValueError, TypeError):
        return (json.dumps(rows, sort_keys=True, ensure_ascii=False, default=str),
                json.dumps(holiday_rows, sort_keys=True, ensure_ascii=False, default=str))


@dataclass
class DiskState:
    """편집기가 마지막으로 읽거나 쓴 순간의 파일 상태."""
    sig: str | None
    timetable: tuple

    @classmethod
    def read(cls, path: str | Path) -> DiskState:
        try:
            rows, hrows = saved_rows(path)
        except tomllib.TOMLDecodeError:
            rows, hrows = [], []
        return cls(fingerprint(path), canonical(rows, hrows))


def disk_change(path: str | Path, base: DiskState) -> str:
    """base 이후 파일이 어떻게 바뀌었나.

    'same'      — 그대로
    'other'     — 바뀌었지만 시간표·공휴일은 같음(다른 설정만). 저장해도 안전:
                  저장은 표시줄 사이만 바꾸므로 바깥 변경은 그대로 남습니다.
    'timetable' — 시간표·공휴일이 바뀜(덮어쓰면 그 변경을 잃음)
    'broken'    — 지금 파일이 올바른 TOML 이 아님(누가 고치는 중일 수 있음)
    """
    sig = fingerprint(path)
    if sig == base.sig:
        return "same"
    try:
        rows, hrows = saved_rows(path)
    except tomllib.TOMLDecodeError:
        return "broken"
    return "other" if canonical(rows, hrows) == base.timetable else "timetable"


def upcoming(rows, holiday_rows, *, now: datetime | None = None, count: int = 5,
             horizon_days: int = 120) -> list[Upcoming]:
    """앞으로 녹화될 수업 count 개와, 그 사이 공휴일로 건너뛰는 회차.

    녹화기(recorder.schedule)와 같은 parse 를 써서 '편집기는 이렇게 보이는데
    녹화기는 다르게 읽는' 일이 없게 합니다. 진행 중인 수업도 포함합니다.
    """
    from ..recorder import schedule as sched

    now = now or datetime.now()
    slots = sched.parse(rows)
    names: dict[date, str] = {}
    for h in holiday_rows or []:
        raw = h.get("date") if isinstance(h, dict) else h
        try:
            d = _as_date(raw)
        except (TypeError, ValueError):
            continue
        names[d] = str(h.get("name", "")) if isinstance(h, dict) else ""

    out: list[Upcoming] = []
    taken = 0
    for offset in range(horizon_days):
        day = (now + timedelta(days=offset)).date()
        todays = sorted((s for s in slots if s.weekday == day.weekday()),
                        key=lambda s: (s.start, s.name))
        for s in todays:
            start, end = s.occurrence(day)
            if end <= now:
                continue
            if day in names:
                out.append(Upcoming(start, end, s.name, names[day]))
            else:
                out.append(Upcoming(start, end, s.name))
                taken += 1
        if taken >= count:
            break
    # 마지막으로 센 수업 뒤의 '건너뜀' 은 잘라냅니다(목록 꼬리가 길어지지 않게).
    while out and out[-1].skipped:
        out.pop()
    return out


def as_dict(tt: Timetable) -> dict:
    """테스트·디버그용."""
    return {"courses": [asdict(c) for c in tt.courses],
            "holidays": [{"day": h.day.isoformat(), "name": h.name} for h in tt.holidays]}
