"""설정값이 어긋날 때 '읽을 수 없습니다' 대신 하는 설명.

원문(예시 파일·config.toml 의 그 줄과 주석 그대로)과 코드가 아는 설정(근거 코드
파일·줄·주석)을 나란히 놓고, 무엇이 바뀐 것 같은지 추측을 적고, 추측일 뿐이니
교차검증을 부탁합니다. 코드가 아는 설정은 config.py 의 DEFAULTS(기본값과 그 위
주석), 검사 줄(_choice/_positive 등), 그 이름을 점 표기로 읽는 곳에서 찾습니다.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config as C
from . import settings_schema as S

MISSING: Any = object()
UNSURE = "확실하지는 않아요. 꼼꼼한 검토가 필요해 보입니다. 교차검증을 실시해주세요."
CROSS_CHECK = (
    "위 ① 원문과 ② 근거 코드 줄을 나란히 열어 한 줄씩 대조하기",
    "명령 창에서 <code>python -m syncnote config check</code> 로 실제 검사 결과 보기",
    "다른 PC·최신 저장소의 config.example.toml 과 이 부분을 비교하기",
)
_PKG = Path(C.__file__).resolve().parent


# ---------------------------------------------------------------------------
# 코드가 아는 설정
# ---------------------------------------------------------------------------
@dataclass
class CodeRef:
    path: str          # 저장소 기준(예: src/syncnote/config.py)
    line: int
    code: str          # 그 줄 원문(앞뒤 공백 정리)
    comments: list[str] = field(default_factory=list)   # 바로 위 주석·줄 끝 주석


@dataclass
class Knowledge:
    dotted: str
    known: bool
    default: Any = MISSING
    default_ref: CodeRef | None = None
    checks: list[CodeRef] = field(default_factory=list)
    uses: list[CodeRef] = field(default_factory=list)
    allowed: list[Any] | None = None


def _rel(p: Path) -> str:
    root = _PKG.parent.parent if _PKG.parent.name == "src" else _PKG.parent
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.name


def _comments_above(lines: list[str], idx: int) -> list[str]:
    out = []
    j = idx - 1
    while j >= 0 and lines[j].strip().startswith("#"):
        out.insert(0, lines[j].strip())
        j -= 1
    m = re.search(r"\s#\s.*$", lines[idx])
    if m and lines[idx].count('"') % 2 == 0:
        out.append(m.group(0).strip())
    return out


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, CodeRef], dict[str, list[CodeRef]], dict[str, list[CodeRef]],
                      dict[str, str]]:
    """(기본값 줄, 검사 줄, 쓰는 줄, _choice 허용 목록 이름) — 소스가 없으면 빈 것."""
    defaults: dict[str, CodeRef] = {}
    checks: dict[str, list[CodeRef]] = {}
    uses: dict[str, list[CodeRef]] = {}
    choice: dict[str, str] = {}
    cfg_path = _PKG / "config.py"
    try:
        src = cfg_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return defaults, checks, uses, choice
    lines = src.splitlines()
    span = (0, 0)
    for node in tree.body:
        target = (node.target if isinstance(node, ast.AnnAssign)
                  else node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "DEFAULTS" \
                and isinstance(node.value, ast.Dict):
            span = (node.lineno, node.end_lineno or node.lineno)

            def walk(d: ast.Dict, prefix: str) -> None:
                for k, v in zip(d.keys, d.values, strict=False):
                    if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                        continue
                    dotted = f"{prefix}.{k.value}" if prefix else k.value
                    if isinstance(v, ast.Dict) and v.keys:
                        walk(v, dotted)
                    idx = k.lineno - 1
                    defaults[dotted] = CodeRef(_rel(cfg_path), k.lineno, lines[idx].strip(),
                                               _comments_above(lines, idx))
            walk(node.value, "")
    lit = re.compile(r"""["']([a-z_]+(?:\.[a-z0-9_]+)+)["']""")
    in_doc = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith(("#", '"""')) or span[0] <= i + 1 <= span[1]:
            continue                               # 설명글·주석·기본값 표는 '검사' 가 아님
        for m in lit.finditer(ln):
            checks.setdefault(m.group(1), []).append(
                CodeRef(_rel(cfg_path), i + 1, ln.strip(), _comments_above(lines, i)))
        m = re.search(r"""_choice\(cfg,\s*["']([\w.]+)["'],\s*(_VALID_\w+)""", ln)
        if m:
            choice[m.group(1)] = m.group(2)
        m = re.search(r"""cfg\.get\(["']([\w.]+)["']\)\s+not in\s+(\{[^}]*\})""", ln)
        if m:
            choice[m.group(1)] = m.group(2)            # 'not in {"mkv", "mp4"}' 처럼 바로 적은 목록
    for p in sorted(_PKG.rglob("*.py")):
        if p.name in ("config.py", "settings_schema.py", "settings_knowledge.py"):
            continue
        try:
            body = p.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, ln in enumerate(body):
            for m in lit.finditer(ln):
                uses.setdefault(m.group(1), []).append(CodeRef(_rel(p), i + 1, ln.strip()))
    return defaults, checks, uses, choice


def knowledge(dotted: str) -> Knowledge:
    defaults, checks, uses, choice = _index()
    try:
        default = S._get(C.DEFAULTS, dotted)
        known = True
    except (KeyError, TypeError):
        default, known = MISSING, False
    allowed = None
    if dotted in choice:
        src = choice[dotted]
        try:
            vals = ast.literal_eval(src) if src.startswith("{") else getattr(C, src, ())
        except (ValueError, SyntaxError):
            vals = ()
        allowed = sorted(vals or ()) or None
    return Knowledge(dotted, known, default, defaults.get(dotted),
                     checks.get(dotted, []), uses.get(dotted, []), allowed)


def code_keys() -> list[str]:
    return list(S._leaf_values(C.DEFAULTS))


# ---------------------------------------------------------------------------
# 원문
# ---------------------------------------------------------------------------
@dataclass
class Original:
    name: str          # 파일 이름
    start: int         # 1부터
    lines: list[str]


def original(name: str, text: str, line: int, end: int = 0) -> Original | None:
    """line(1부터) 줄의 키와 바로 위 주석·값 전체·아래 들여쓴 주석을 원문 그대로."""
    lines = text.split("\n")
    i = line - 1
    if not 0 <= i < len(lines):
        return None
    a = i
    while a > 0 and lines[a - 1].strip().startswith("#"):
        a -= 1
    b = max(end - 1, S._value_span(lines, i) if S._KEY.match(lines[i]) else i)
    while b + 1 < len(lines) and lines[b + 1][:1] in (" ", "\t") \
            and lines[b + 1].strip().startswith("#"):
        b += 1
    return Original(name, a + 1, lines[a:b + 1])


def locate(text: str, dotted: str) -> int:
    """설정 본문에서 dotted 키가 적힌 줄(1부터). 없으면 0."""
    section = ""
    want_sec, _, want_key = dotted.rpartition(".")
    for i, raw in enumerate(text.split("\n")):
        s = raw.strip()
        h = S._HEADER.match(s)
        if h:
            section = h.group(1)
            continue
        if S._ARRAY_HEADER.match(s):
            section = "\0"
            continue
        k = S._KEY.match(raw)
        if k and section == want_sec and k.group(1) == want_key:
            return i + 1
    return 0


# ---------------------------------------------------------------------------
# 추측
# ---------------------------------------------------------------------------
def kind_ko(v: Any) -> str:
    if v is MISSING:
        return "없음"
    if isinstance(v, bool):
        return "켬/끔(true/false)"
    if isinstance(v, (int, float)):
        return "숫자"
    if isinstance(v, str):
        return '글자("따옴표")'
    if isinstance(v, list):
        return "목록 [ ]"
    if isinstance(v, dict):
        return "표 { }"
    return type(v).__name__


def _same_kind(a: Any, b: Any) -> bool:
    return kind_ko(a) == kind_ko(b)


def show(v: Any) -> str:
    if v is MISSING:
        return "(없음)"
    try:
        return S.render(v)
    except S.SchemaError:
        return repr(v)


_KO_NUM = {w: n for n, ws in enumerate((
    (), ("하나", "한", "일"), ("둘", "두", "이"), ("셋", "세", "삼"), ("넷", "네", "사"),
    ("다섯", "오"), ("여섯", "육"), ("일곱", "칠"), ("여덟", "팔"), ("아홉", "구"), ("열", "십")))
    for w in ws}
_KO_NUM.update({"영": 0, "공": 0, "스물": 20, "서른": 30, "백": 100})
_TRUE = {"true", "켬", "켜기", "on", "yes", "y", "예", "네", "1", "참"}
_FALSE = {"false", "끔", "끄기", "off", "no", "n", "아니오", "아니요", "0", "거짓"}


def coerce(v: Any, like: Any) -> Any:
    """v 를 like 와 같은 종류로 읽어 볼 때 가장 그럴듯한 값. 모르면 MISSING."""
    if like is MISSING or _same_kind(v, like):
        return MISSING
    text = v if isinstance(v, str) else None
    if isinstance(like, bool):
        if text is not None and text.strip().lower() in _TRUE:
            return True
        if text is not None and text.strip().lower() in _FALSE:
            return False
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        return MISSING
    if isinstance(like, (int, float)):
        if text is not None and text.strip() in _KO_NUM:
            return _KO_NUM[text.strip()]             # '열' → 10
        if text is not None:
            m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z가-힣%]*)\s*", text)
            if m:
                n = float(m.group(1))
                return int(n) if n.is_integer() and isinstance(like, int) else n
        return MISSING
    if isinstance(like, str):
        if isinstance(v, (bool, int, float)):
            return S.render(v) if isinstance(v, bool) else str(v)
        if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
            return v[0]
        return MISSING
    if isinstance(like, list):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return [v]
    return MISSING


def raw_value(line_text: str) -> str:
    """`key = 값  # 주석` 줄에서 값 부분 원문('=' 가 없으면 키 뒤 글자)."""
    s = line_text.strip()
    if s.startswith(S.BROKEN_MARK.strip()):
        s = s[len(S.BROKEN_MARK.strip()):].strip()
    if "=" in s:
        rest = s.split("=", 1)[1]
    else:
        parts = s.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    return S._split_inline(rest)[0].strip()


def guess_from_raw(raw: str, like: Any) -> Any:
    """깨진 줄의 값 원문으로 원래 뜻을 짐작(코드가 기다리는 종류 기준)."""
    if like is MISSING or not raw:
        return MISSING
    t = raw.strip()
    if isinstance(like, str):
        t = t.strip("\"'")
        return t if t else MISSING
    unq = t.strip("\"'")
    return coerce(unq, like)


def _similar(key: str, pool: list[str]) -> list[str]:
    near = difflib.get_close_matches(key, pool, n=2, cutoff=0.6)
    # crf → crf_value 처럼 한쪽이 다른 쪽을 품으면 철자 거리가 멀어도 같은 설정일 수 있음
    near += [p for p in pool if p not in near and len(min(p, key, key=len)) >= 3
             and (p in key or key in p)]
    return near[:3]


@dataclass
class Report:
    dotted: str
    originals: list[Original]
    know: Knowledge
    guesses: list[str]
    suggestion: Any = MISSING      # '추측한 값 넣기' 로 넣을 값(없으면 MISSING)
    suggestion_why: str = ""

    def __bool__(self) -> bool:
        return bool(self.guesses)


def analyze(dotted: str, *, example_value: Any = MISSING, example_keys: set[str] | None = None,
            current_value: Any = MISSING, typed: str | None = None,
            issues: list[str] | None = None,
            broken: list[tuple[str, S.Broken]] | None = None, shown: Any = MISSING,
            originals: list[Original] | None = None) -> Report:
    """이 설정에서 어긋난 것들과 '이렇게 바뀐 것 같다' 는 추측."""
    k = knowledge(dotted)
    g: list[str] = []
    sug, sug_why = MISSING, ""
    sec, _, key = dotted.rpartition(".")
    code = f"{k.default_ref.path}:{k.default_ref.line}" if k.default_ref else "config.py DEFAULTS"
    placeholder = k.default in ("", [], {})

    for name, b in broken or []:
        raw = raw_value(b.text)
        g.append(f"{S_esc(name)} {b.line}번째 줄을 TOML 로 읽지 못해({b.why}) 이 설정을 빼고 "
                 f"읽었어요. 지금 화면의 값 <code>{S_esc(show(shown))}</code> 은 원문 값이 아니에요.")
        guess = guess_from_raw(raw, k.default if k.known else shown)
        if guess is not MISSING:
            g.append(f"원문 값 <code>{S_esc(raw)}</code> 을 코드가 기다리는 {kind_ko(k.default)} "
                     f"값으로 읽으면 <code>{S_esc(show(guess))}</code> 인 것 같아요.")
            sug, sug_why = guess, "원문 값을 코드가 기다리는 종류로 읽은 값"
        elif raw:
            g.append(f"원문 값 <code>{S_esc(raw)}</code> 이 무엇을 뜻하는지 짐작하지 못했어요. "
                     f"코드는 {kind_ko(k.default)} 값을 기다립니다({code}).")

    if not k.known:
        pool = [d for d in code_keys() if d.rpartition(".")[0] == sec]
        gone = [d for d in pool if example_keys is not None and d not in example_keys]
        near = _similar(key, [d.rpartition(".")[2] for d in gone]) or \
            _similar(key, [d.rpartition(".")[2] for d in pool])
        if not near and len(gone) == 1:
            near = [gone[0].rpartition(".")[2]]        # 이 섹션에서 예시에서 사라진 게 하나뿐
        if near:
            g.append("코드에는 이 이름이 없어요. 코드가 아는 "
                     + ", ".join(f"<code>{sec}.{n}</code>" for n in near)
                     + " 의 이름이 바뀌었거나 오타인 것 같아요. 코드가 아직 이 이름을 읽지 "
                     "않으니 지금은 이 값이 아무 효과가 없을 수 있어요.")
        else:
            g.append("코드(config.py DEFAULTS)에 이 이름이 없어요. 예시 파일에 새 설정이 먼저 "
                     "들어오고 코드는 아직 옛 버전이거나, 쓰지 않게 된 설정인 것 같아요. "
                     "지금 코드에서는 이 값이 아무 효과가 없을 수 있어요.")
    elif example_value is not MISSING:
        if not _same_kind(example_value, k.default) and not placeholder:
            c = coerce(example_value, k.default)
            msg = (f"값 종류가 코드의 {kind_ko(k.default)}(<code>{show(k.default)}</code>, "
                   f"{code}) 에서 예시의 {kind_ko(example_value)}(<code>{show(example_value)}"
                   "</code>) 로 바뀐 것 같아요.")
            if c is not MISSING:
                msg += f" 따옴표를 잘못 붙이거나 뺀 것이라면 원래 뜻은 <code>{show(c)}</code> 일 거예요."
            else:
                msg += " 코드는 아직 옛 종류로 읽으니 이대로 쓰면 오류가 나거나 무시될 수 있어요."
            g.append(msg)
        elif example_value != k.default and not (placeholder and example_value not in ("", [], {})):
            g.append(f"예시의 기본값 <code>{show(example_value)}</code> 이 코드 기본값 "
                     f"<code>{show(k.default)}</code>({code}) 과 달라요. 예시 파일의 기본값이 "
                     "바뀐 것 같아요. config.toml 에 이 줄이 없으면 실제로는 코드 기본값이 "
                     "쓰입니다.")
        if k.allowed is not None and example_value not in k.allowed:
            near = _similar(str(example_value), [str(a) for a in k.allowed])
            g.append(f"예시 값 <code>{show(example_value)}</code> 이 코드가 허용하는 값 "
                     f"{', '.join(f'<code>{a}</code>' for a in k.allowed)} 에 없어요. "
                     + (f"<code>{near[0]}</code> 의 오타이거나 " if near else "")
                     + "코드에 새 선택지가 아직 안 들어간 것 같아요.")

    if current_value is not MISSING and k.known and not placeholder \
            and not _same_kind(current_value, k.default):
        c = coerce(current_value, k.default)
        msg = (f"config.toml 의 값 <code>{show(current_value)}</code> 은 {kind_ko(current_value)} "
               f"인데, 코드는 {kind_ko(k.default)} 값을 기다려요({code}).")
        if c is not MISSING:
            msg += f" 원래 적으려던 값은 <code>{S_esc(show(c))}</code> 인 것 같아요."
            sug, sug_why = c, "config.toml 값을 코드가 기다리는 종류로 읽은 값"
        g.append(msg)

    if typed is not None:
        like = k.default if k.known else example_value
        c = coerce(typed, like)
        msg = (f"입력한 <code>{S_esc(typed)}</code> 을 {kind_ko(like)} 값으로 읽지 못했어요(코드 기본값 "
               f"<code>{show(k.default)}</code>, {code}).")
        if c is not MISSING:
            msg += f" 원래 뜻은 <code>{S_esc(show(c))}</code> 인 것 같아요."
        elif isinstance(like, (list, dict)):
            msg += " 대괄호·따옴표·쉼표가 빠진 것 같아요(예: <code>[\"a\", \"b\"]</code>)."
        g.append(msg)

    for p in issues or []:
        extra = ""
        if k.allowed is not None and current_value is not MISSING:
            near = _similar(str(current_value), [str(a) for a in k.allowed])
            if near:
                extra = (f" 허용 값 중 철자가 가장 비슷한 것은 <code>{S_esc(near[0])}</code> 예요. "
                         "원래 적으려던 값이 이것인 것 같아요.")
                if sug is MISSING:
                    sug, sug_why = near[0], "허용 값 중 가장 비슷한 것"
        g.append(f"코드의 설정 검사가 이렇게 말해요: {S_esc(p)}.{extra}")

    return Report(dotted, originals or [], k, g, sug, sug_why)


def S_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# 화면용 글
# ---------------------------------------------------------------------------
def _pre(lines: list[str], start: int) -> str:
    w = len(str(start + len(lines)))
    body = "\n".join(f"{start + i:>{w}} │ {S_esc(ln)}" for i, ln in enumerate(lines))
    return ("<pre style='margin:2px 0 6px 0; padding:4px; background:#F3F4F6; color:#111827;"
            f" white-space:pre-wrap'>{body}</pre>")


def _ref(r: CodeRef, with_comments: bool = True) -> str:
    out = f"<div><code>{S_esc(r.path)}:{r.line}</code> &nbsp; <code>{S_esc(r.code)}</code></div>"
    if with_comments and r.comments:
        out += ("<pre style='margin:0 0 4px 16px; color:#4B5563; white-space:pre-wrap'>"
                + S_esc("\n".join(r.comments)) + "</pre>")
    return out


def html(rep: Report, *, max_uses: int = 3) -> str:
    k = rep.know
    parts = ["<div style='font-weight:bold'>⚠ 설정값이 어긋나 보여요 — 아래는 제 추측입니다</div>"]

    parts.append("<div style='margin-top:6px'><b>① 원문</b> (파일에 적힌 그대로, 주석 포함)</div>")
    if rep.originals:
        for o in rep.originals:
            parts.append(f"<div>{S_esc(o.name)} {o.start}번째 줄부터</div>" + _pre(o.lines, o.start))
    else:
        parts.append("<div style='color:#6B7280'>(이 설정이 적힌 줄을 찾지 못했어요)</div>")

    parts.append("<div style='margin-top:6px'><b>② 제가 알고 있는 설정</b> (근거 코드와 주석)</div>")
    if not k.known:
        parts.append("<div>코드의 기본값 목록(config.py DEFAULTS)에 이 이름이 없습니다.</div>")
    else:
        parts.append(f"<div>기본값 <code>{S_esc(show(k.default))}</code> · 종류 "
                     f"{S_esc(kind_ko(k.default))}"
                     + (f" · 허용 값 {', '.join(f'<code>{a}</code>' for a in k.allowed)}"
                        if k.allowed else "") + "</div>")
        if k.default_ref:
            parts.append(_ref(k.default_ref))
        else:
            parts.append("<div style='color:#6B7280'>(코드 파일을 찾지 못해 줄 번호는 없습니다)</div>")
    for r in k.checks[:max_uses]:
        parts.append("<div>검사: </div>" + _ref(r, with_comments=False))
    if k.uses:
        parts.append("<div>쓰는 곳:</div>" + "".join(_ref(r, False) for r in k.uses[:max_uses])
                     + (f"<div style='color:#6B7280'>… 외 {len(k.uses) - max_uses}곳</div>"
                        if len(k.uses) > max_uses else ""))
    elif k.known:
        parts.append("<div style='color:#6B7280'>쓰는 곳: 점 표기로 읽는 줄은 못 찾았어요"
                     "(섹션째 읽는 코드가 있을 수 있음).</div>")

    parts.append("<div style='margin-top:6px'><b>③ 그래서 이런 것이 바뀐 것 같아요</b> (추측)</div>")
    parts.append("<ul style='margin:2px 0 0 -20px'>" + "".join(f"<li>{x}</li>" for x in rep.guesses)
                 + "</ul>")

    parts.append(f"<div style='margin-top:6px'><b>④ {UNSURE}</b></div>")
    parts.append("<ol style='margin:2px 0 0 -20px'>" + "".join(f"<li>{x}</li>" for x in CROSS_CHECK)
                 + "</ol>")
    return "".join(parts)
