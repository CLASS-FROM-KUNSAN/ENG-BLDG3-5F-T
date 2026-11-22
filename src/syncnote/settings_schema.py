"""config.example.toml 을 읽어 '설정 목록' 으로 구조화하기.

설정 목록을 코드에 하드코딩하지 않습니다. 항목·기본값·설명·섹션 제목·소분류를
전부 예시 파일에서 읽습니다. 예시 파일이 바뀌면 화면도 따라 바뀝니다.

  - 값:   tomllib 로 정확히 읽음(문자열 이스케이프·여러 줄 목록까지)
  - 설명: 키 바로 위 주석 + 줄 끝 주석 + 키 아래 들여쓴 이어지는 주석
  - 섹션 제목: 섹션 머리 주석(예: `# [general] 공통 설정`, `#  1. ECLASS 수집`)
  - 소분류: 섹션 안의 `# --- 학습 목차 ---` 같은 줄
  - 주석으로 막아 둔 예시(`# [[eclass.course_repos]]`)와 `[[표 배열]]` 은 제외

새 config.toml 은 예시 파일 **본문 그대로**에 사용자가 바꾼 값만 그 줄에
바꿔 넣어 만듭니다. 주석과 순서가 그대로 남아, 나중에 손으로 열어도
설명을 읽을 수 있습니다. 쓴 뒤 다시 읽어 모든 값이 의도대로인지 대조합니다.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

_HEADER = re.compile(r"^\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*(#.*)?$")
_ARRAY_HEADER = re.compile(r"^\[\[\s*([A-Za-z0-9_.\-]+)\s*\]\]")
_KEY = re.compile(r"^([A-Za-z0-9_\-]+)(\s*)=(\s*)(.*)$")
_DECOR = re.compile(r"^#\s*[-=]{5,}\s*$")
_SUBGROUP = re.compile(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$")
_TITLE_NOISE = re.compile(r"^\[[^\]]+\]\s*|^\d+(?:-\d+)*\.\s*")


class SchemaError(ValueError):
    """예시 파일을 해석하거나 새 설정을 만들 수 없습니다."""


@dataclass
class Field:
    section: str            # "recording.video"
    key: str                # "fps"
    value: Any              # 예시 파일의 값(=기본값)
    doc: str = ""           # 위 주석 + 줄 끝 주석, 정리된 여러 줄
    group: str = ""         # 섹션 안 소분류("학습 목차")
    line: int = 0           # 예시 파일에서의 줄 번호(1부터)
    raw: list[str] = field(default_factory=list)   # 설명 주석 원문('#' 뒤, 들여쓰기 그대로)

    @property
    def dotted(self) -> str:
        return f"{self.section}.{self.key}" if self.section else self.key

    @property
    def kind(self) -> str:
        v = self.value
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "str"
        if isinstance(v, list) and all(isinstance(x, (str, int, float)) and not isinstance(x, bool)
                                       for x in v):
            return "list"
        return "other"

    @property
    def title(self) -> str:
        """설명 첫 문장(없으면 키 이름)."""
        first = self.doc.strip().splitlines()[0] if self.doc.strip() else ""
        return first or self.key


@dataclass
class Section:
    path: str
    title: str
    doc: str = ""
    fields: list[Field] = field(default_factory=list)
    line: int = 0

    @property
    def top(self) -> str:
        return self.path.split(".")[0]


# ---------------------------------------------------------------------------
# 해석
# ---------------------------------------------------------------------------
def _clean_comment(line: str) -> str:
    s = line.strip()
    if s.startswith("#"):
        s = s[1:]
    return s.strip()


def _raw_comment(line: str) -> str:
    """'#' 뒤 글자를 들여쓰기 그대로(켬/끔 설명의 이어지는 줄을 가리는 데 씀)."""
    s = line.strip()
    if s.startswith("#"):
        s = s[1:]
    return s.rstrip()


def _split_inline(rest: str) -> tuple[str, str]:
    """`값  # 주석` 에서 값과 주석을 가름(따옴표 안의 # 는 무시)."""
    quote = None
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return rest[:i].rstrip(), rest[i + 1:].strip()
        i += 1
    return rest.rstrip(), ""


def _balance(text: str) -> int:
    """따옴표 밖의 [ ] { } 균형."""
    depth, quote, i = 0, None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


def _value_span(lines: list[str], i: int) -> int:
    """i 번째 줄의 `key = 값` 이 끝나는 줄 번호(여러 줄 목록·여러 줄 문자열)."""
    m = _KEY.match(lines[i])
    rest = m.group(4) if m else ""
    if rest.lstrip().startswith(('"""', "'''")):
        q = rest.lstrip()[:3]
        if rest.lstrip().count(q) >= 2:
            return i
        j = i + 1
        while j < len(lines) and q not in lines[j]:
            j += 1
        return min(j, len(lines) - 1)
    depth = _balance(rest)
    j = i
    while depth > 0 and j + 1 < len(lines):
        j += 1
        depth += _balance(lines[j])
    return j


def _title_from(doc_lines: list[str], path: str) -> str:
    for ln in doc_lines:
        t = _TITLE_NOISE.sub("", ln).strip()
        t = _TITLE_NOISE.sub("", t).strip()
        if t:
            return t
    return path.split(".")[-1]


def parse(text: str) -> list[Section]:
    """예시 파일 → 섹션 목록(파일 순서)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"config.example.toml 이 올바른 TOML 이 아닙니다: {exc}") from exc

    lines = text.splitlines()
    sections: list[Section] = []
    current: Section | None = Section("", "기본", line=0)   # 첫 머리 앞 키(드묾)
    sections.append(current)
    buf: list[str] = []          # 지금 쌓이는 주석 블록
    braw: list[str] = []         # 같은 블록의 원문(들여쓰기 그대로)
    last_above_raw: list[str] = []
    group = ""
    last_above = ""            # 바로 앞 키의 '위 주석'(붙어 있는 다음 키가 물려받음)
    after_key = False            # 바로 앞 줄이 키 줄이었나(빈 줄 없이 이어진 키 묶음)
    untitled: set[int] = set()   # 머리 주석이 없어 본문 첫 주석으로 제목을 정할 섹션
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            buf, braw = [], []
            after_key = False
            i += 1
            continue
        if s.startswith("#"):
            if raw[:1] in (" ", "\t") and current is not None and current.fields \
                    and current.fields[-1].line and i > 0 and lines[i - 1].strip():
                # 키 아래 들여쓴 이어지는 주석 → 그 키의 설명
                prev = current.fields[-1]
                prev.doc = (prev.doc + "\n" + _clean_comment(s)).strip()
                prev.raw.append(_raw_comment(s))
                i += 1
                continue
            sub = _SUBGROUP.match(s)
            if sub and not _DECOR.match(s):
                group = sub.group(1).strip()
                buf, braw = [], []
            elif not _DECOR.match(s):
                buf.append(_clean_comment(s))
                braw.append(_raw_comment(s))
            after_key = False
            i += 1
            continue
        if _ARRAY_HEADER.match(s):
            current = None           # [[표 배열]] 안의 키는 다루지 않음
            buf, braw = [], []
            i += 1
            continue
        h = _HEADER.match(s)
        if h:
            path = h.group(1)
            current = Section(path, _title_from(buf, path), "\n".join(buf[1:]).strip(), line=i + 1)
            if not buf:
                untitled.add(id(current))
            sections.append(current)
            after_key = False
            buf, braw, group = [], [], ""
            i += 1
            continue
        k = _KEY.match(raw)
        if k and current is not None:
            end = _value_span(lines, i)
            key = k.group(1)
            node: Any = data
            for part in (current.path.split(".") if current.path else []):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            if isinstance(node, dict) and key in node:
                _val, inline = _split_inline(k.group(4)) if end == i else ("", "")
                above = "\n".join(buf)
                above_raw = list(braw)
                if not above and after_key:
                    above = last_above       # owner/repo 처럼 붙어 쓴 키는 위 설명을 같이 씀
                    above_raw = list(last_above_raw)
                last_above, last_above_raw = above, above_raw
                doc = above
                if not doc and key == "enabled":
                    doc = f"{current.title} 켜기"   # 설명 없는 on/off 스위치에 읽을 이름
                if inline:
                    doc = (doc + ("\n" if doc else "") + inline).strip()
                    above_raw = above_raw + [inline]
                current.fields.append(Field(current.path, key, node[key], doc, group, i + 1,
                                            raw=above_raw))
            buf, braw = [], []
            after_key = True
            i = end + 1
            continue
        buf, braw = [], []
        i += 1
    for s in sections:
        if id(s) in untitled and s.fields and s.fields[0].doc:
            first = s.fields[0].doc.splitlines()[0]
            s.title = first if len(first) <= 40 else first[:39] + "…"
    return [s for s in sections if s.fields]


# ---------------------------------------------------------------------------
# 켬/끔 설명 — 예시 파일의 '# true = 켬 — …' / '# false = 끔 — …' 를 그대로 읽음
# ---------------------------------------------------------------------------
# 예시 파일의 약속(이 모양이면 설정 창이 스위치 옆에 바로 보여 줍니다):
#     #   true  = 켬 — 켜면 일어나는 일   [기본]
#     #   false = 끔 — 끄면 일어나는 일
#     #           (더 들여 쓴 줄은 바로 위 줄의 이어지는 설명)
# 한 줄로 적어도 됩니다:  # true = 켬 — A / false = 끔 — B [기본]
_ON = re.compile(r"^(\s*)true\s*=\s*켬(.*)$", re.I)
_OFF = re.compile(r"^(\s*)false\s*=\s*끔(.*)$", re.I)
_SPLIT_OFF = re.compile(r"\s*[/,]\s*(?=false\s*=\s*끔)", re.I)
_DEFAULT = re.compile(r"\[\s*기본[^\]]*\]")


@dataclass
class Toggle:
    on: str                  # 켜면 일어나는 일(없으면 "")
    off: str                 # 끄면 일어나는 일
    default: bool | None     # 예시 파일이 [기본] 으로 표시한 쪽(없으면 None)
    used: set[int]           # 켬/끔 설명에 쓴 raw 줄 번호 — 나머지가 일반 설명

    @property
    def complete(self) -> bool:
        return bool(self.on and self.off)


def _clean_desc(text: str) -> tuple[str, bool]:
    """'— 버림   [기본]' -> ('버림', True). 앞 구분 기호·괄호·여분 공백 정리."""
    is_default = bool(_DEFAULT.search(text))
    s = _DEFAULT.sub("", text)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip("—–-:·").strip()
    if s.startswith("(") and s.endswith(")") and s.count("(") == 1:
        s = s[1:-1].strip()
    s = re.sub(r"\s+([.,)])", r"\1", s).strip(" .")
    return s, is_default


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def toggle(f: Field) -> Toggle:
    """true/false 설정의 켬·끔 설명. 예시 파일 주석에서만 읽습니다(코드에 적어 두지 않음)."""
    on_parts: list[str] = []
    off_parts: list[str] = []
    default: bool | None = None
    used: set[int] = set()
    lines = f.raw
    i = 0
    while i < len(lines):
        line = lines[i]
        m_on, m_off = _ON.match(line), _OFF.match(line)
        m = m_on or m_off
        if not m:
            i += 1
            continue
        used.add(i)
        base = _indent(line)
        body = m.group(2)
        cont = []
        j = i + 1
        while j < len(lines) and lines[j].strip() and _indent(lines[j]) > base \
                and not _ON.match(lines[j]) and not _OFF.match(lines[j]):
            cont.append(lines[j].strip())
            used.add(j)
            j += 1
        if m_on and _SPLIT_OFF.search(body):            # 한 줄에 둘 다
            on_body, off_line = _SPLIT_OFF.split(body, maxsplit=1)
            off_body = _OFF.match(off_line).group(2)
            pieces = [(on_body, True), (" ".join([off_body, *cont]), False)]
        else:
            pieces = [(" ".join([body, *cont]), bool(m_on))]
        for text, is_on in pieces:
            desc, is_def = _clean_desc(text)
            (on_parts if is_on else off_parts).append(desc)
            if is_def:
                default = is_on
        i = j
    return Toggle(" ".join(x for x in on_parts if x).strip(),
                  " ".join(x for x in off_parts if x).strip(), default, used)


def help_without_toggle(f: Field) -> str:
    """켬/끔 줄을 뺀 나머지 설명(첫 줄 = 제목도 뺌)."""
    if f.kind != "bool" or not f.raw:
        return "\n".join(f.doc.splitlines()[1:]).strip()
    tg = toggle(f)
    rest = [ln.strip() for k, ln in enumerate(f.raw) if k not in tg.used and ln.strip()]
    if rest and rest[0] == f.title:
        rest = rest[1:]
    return "\n".join(rest).strip()


# ---------------------------------------------------------------------------
# 사용자가 읽기 편한 순서로 묶기
# ---------------------------------------------------------------------------
# 표시 순서만 정합니다(항목 자체는 예시 파일에서 옴). 여기 없는 최상위 섹션은
# 예시 파일 순서대로 뒤에 붙습니다. 처음 쓰는 사람이 위에서부터 채우면 되는
# 순서: 공통 → 수집 → 업로드 → 녹화 → 전사 → 송출 → 백업 → 활동 기록.
FRIENDLY_ORDER = ("general", "eclass", "github", "recording", "transcribe", "stream",
                  "backup", "activity")


@dataclass
class Category:
    top: str
    title: str
    sections: list[Section]


def categories(sections: list[Section]) -> list[Category]:
    by_top: dict[str, list[Section]] = {}
    for s in sections:
        by_top.setdefault(s.top, []).append(s)
    order = [t for t in FRIENDLY_ORDER if t in by_top] + \
            [t for t in by_top if t not in FRIENDLY_ORDER]
    out = []
    for top in order:
        secs = by_top[top]
        head = next((s for s in secs if s.path == top), None)
        out.append(Category(top, head.title if head else secs[0].title, secs))
    return out


# ---------------------------------------------------------------------------
# 값 → TOML 글자
# ---------------------------------------------------------------------------
def render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        s = repr(value)
        return s if any(c in s for c in ".en") else s + ".0"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(render(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {render(v)}" for k, v in value.items()) + " }"
    raise SchemaError(f"TOML 로 쓸 수 없는 값: {value!r}")


# ---------------------------------------------------------------------------
# 예시 파일 + 바꾼 값 → 새 config.toml 본문
# ---------------------------------------------------------------------------
def _get(data: dict, dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _leaf_values(data: dict, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        dotted = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_leaf_values(v, dotted))
        else:
            out[dotted] = v
    return out


def current_values(text: str) -> dict[str, Any]:
    """설정 본문의 '값 하나짜리' 키들(점 표기). 읽을 수 없으면 SchemaError."""
    try:
        return _leaf_values(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"설정 파일이 올바른 TOML 이 아닙니다: {exc}") from exc


_MANAGED_BLOCK = "# >>> syncnote 시간표 편집기"   # timetable.model.BEGIN 의 앞부분


def apply(text: str, changes: dict[str, Any], known: set[str] | None = None) -> str:
    """설정 본문(예시든 기존 config.toml 이든)에 changes 를 넣은 새 본문.

    - 본문에 있는 키: 그 줄의 값만 바꿈(주석·순서·다른 줄은 그대로, 여러 줄
      값은 한 줄로).
    - 본문에 없는 키(known 에 있을 때만): 그 섹션의 마지막 키 아래에 넣고,
      섹션이 없으면 새 섹션을 만듭니다. 시간표 편집기 블록보다 앞에 둡니다.
    결과를 다시 읽어 바꾼 값은 새 값, 나머지 값은 원래 값과 같은지 확인합니다.
    """
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"설정 파일이 올바른 TOML 이 아닙니다: {exc}") from exc
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    in_array = False
    done: set[str] = set()
    seen_sections: dict[str, int] = {"": -1}     # 섹션 → 그 섹션 마지막 키 줄(out 기준)
    block_at: int | None = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if block_at is None and s.startswith(_MANAGED_BLOCK):
            block_at = len(out)
        if _ARRAY_HEADER.match(s):
            in_array = True
        elif _HEADER.match(s):
            section, in_array = _HEADER.match(s).group(1), False
            seen_sections.setdefault(section, len(out))   # 키가 없으면 머리 줄 바로 뒤
        k = _KEY.match(raw) if not s.startswith("#") else None
        if k and not in_array:
            dotted = f"{section}.{k.group(1)}" if section else k.group(1)
            end = _value_span(lines, i)
            if dotted in changes:
                _v, inline = _split_inline(k.group(4)) if end == i else ("", "")
                new = f"{k.group(1)}{k.group(2)}={k.group(3) or ' '}{render(changes[dotted])}"
                if inline:
                    new += f"  # {inline}"
                out.append(new)
                done.add(dotted)
            else:
                out.extend(lines[i:end + 1])
            seen_sections[section] = len(out) - 1
            i = end + 1
            continue
        out.append(raw)
        i += 1

    missing = [d for d in changes if d not in done]
    unknown = [d for d in missing if known is None or d not in known]
    if unknown:
        raise SchemaError(f"설정 목록에 없는 항목입니다: {sorted(unknown)}")
    # 없는 키 넣기: 아래쪽부터 넣어야 앞쪽 위치가 안 밀립니다.
    by_section: dict[str, list[str]] = {}
    for d in missing:
        sec, _, key = d.rpartition(".")
        by_section.setdefault(sec, []).append(f"{key} = {render(changes[d])}")
    inserts: list[tuple[int, list[str]]] = []
    tail: list[str] = []
    for sec, kv in by_section.items():
        if sec in seen_sections:
            inserts.append((seen_sections[sec] + 1, kv))
        else:
            tail += ["", f"[{sec}]", *kv]
    for pos, kv in sorted(inserts, key=lambda t: -t[0]):
        out[pos:pos] = kv
        if block_at is not None and pos <= block_at:
            block_at += len(kv)
    if tail:
        pos = block_at if block_at is not None else len(out)
        out[pos:pos] = tail + ([""] if block_at is not None else [])
    result = "\n".join(out) + ("\n" if text.endswith("\n") or tail else "")

    try:
        parsed = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise SchemaError(f"만든 설정이 올바른 TOML 이 아닙니다: {exc}") from exc
    for dotted, want in changes.items():
        try:
            got = _get(parsed, dotted)
        except KeyError:
            raise SchemaError(f"{dotted} 를 넣었는데 다시 읽으니 없습니다") from None
        if got != want:
            raise SchemaError(f"{dotted} 를 바꿔 넣었는데 다시 읽으니 값이 다릅니다")
    after = _leaf_values(parsed)
    for dotted, was in _leaf_values(original).items():
        if dotted not in changes and after.get(dotted, object()) != was:
            raise SchemaError(f"{dotted} 가 의도치 않게 바뀌었습니다")
    return result

def problems(text: str) -> list[str]:
    """이 본문을 config.toml 로 쓰면 나올 설정 오류 목록(실제 검증 그대로)."""
    from . import config as config_module

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"TOML 문법 오류: {exc}"]
    cfg = config_module.Config(data=config_module._deep_merge(config_module.DEFAULTS, raw))
    return config_module.problems(cfg)


def fields_in(message: str, fields: list[Field]) -> list[Field]:
    """오류 문장에 나오는 설정 항목들(긴 이름 먼저 맞춰 'a.b' 가 'a.bc' 에 걸리지 않게)."""
    hits = []
    for f in sorted(fields, key=lambda f: -len(f.dotted)):
        if re.search(rf"(?<![\w.]){re.escape(f.dotted)}(?![\w])", message):
            hits.append(f)
    return hits
