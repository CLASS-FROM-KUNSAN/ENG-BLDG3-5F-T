"""텍스트 추출본 옆에 그 화면의 스크린샷을 남깁니다.

`.md` 는 innerText 입니다. 표의 칸 구분, 굵게·색으로 강조한 마감일,
본문에 붙은 그림, 줄 바꿈 위치는 글자로 옮기면서 사라집니다. 같은
이름의 `.png` 를 옆에 두면 "원래 어떻게 생겼었나" 를 나중에 볼 수
있고, 추출이 무언가를 빠뜨렸을 때 그것을 알아챌 수 있습니다.

두 가지는 텍스트 쪽과 똑같이 지킵니다.

1. **`.md` 에 없는 개인정보를 그림으로 새로 내보내지 않습니다.**
   모든 화면 머리에 로그인한 본인의 '학번(이름)님' 이 찍힙니다.
   `.md` 에는 안 들어가는 정보라 그림에서도 가립니다(mask).
2. **쪽지 보낸이는 `.md` 와 같은 이름표로 바꾼 뒤에 찍습니다.**
   보낸이 칸은 `김진욱 (101394)` 처럼 이름과 사번이 함께 있고,
   `.md` 는 이것을 `보낸이-3f9a1c` 로 익명화합니다. 그림이 원문을
   그대로 보여 주면 그 익명화가 무의미해집니다. 바꾼 뒤에도 원문이
   화면에 남아 있으면 **찍지 않습니다**(실패 시 닫힘).

스크린샷은 덤입니다. 실패해도 글 수집은 그대로 성공입니다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ..logging_setup import get as get_logger
from . import anon

log = get_logger("eclass.snapshot")

# 로그인한 본인의 '학번(이름)님' (main_logged_in.html 기준).
SELF_INFO = "div.login .info, #loginForm .info"

# 화면 글자 속의 '이름 (사번)'. anon.sender 가 받는 꼴과 같습니다.
# 이름에 공백을 허용하지 않습니다 — 허용하면 "보낸이 김진욱 (101394)" 의
# '보낸이 ' 까지 먹어서 목록의 같은 사람과 다른 이름표가 나옵니다.
SENDER_PATTERN = r"[가-힣A-Za-z]{2,20}\s*\(\s*\d{4,}\s*\)"

_FIND = """(pat) => {
  const re = new RegExp(pat, 'g');
  const found = new Set();
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    for (const m of (n.nodeValue || '').matchAll(re)) found.add(m[0]);
  }
  return Array.from(found);
}"""

_REPLACE = """(pairs) => {
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    let v = n.nodeValue || '';
    let changed = false;
    for (const [a, b] of pairs) {
      if (a && v.includes(a)) { v = v.split(a).join(b); changed = true; }
    }
    if (changed) n.nodeValue = v;
  }
  for (const el of document.querySelectorAll('input, textarea')) {
    let v = el.value || '';
    for (const [a, b] of pairs) { if (a && v.includes(a)) v = v.split(a).join(b); }
    el.value = v;
  }
}"""

_VISIBLE_TEXT = """() => {
  const vals = Array.from(document.querySelectorAll('input, textarea')).map(e => e.value || '');
  return (document.body ? document.body.innerText : '') + '\\n' + vals.join('\\n');
}"""

# 화면 글자 속 '확실한 PII 토큰'(이름(사번)·이메일·전화·학번/사번)과 본인
# 이름을 그 자리에서 가립니다. singlefile 의 마스킹과 같은 규칙·같은 대괄호
# 플레이스홀더입니다(각괄호는 브라우저가 알 수 없는 태그로 보고 숨깁니다).
# 쪽지의 '보낸이' 는 anonymize_senders 가 먼저 안정적 이름표로 바꾸므로 여기
# 선 건드릴 게 남지 않습니다. 사번 없는 제3자 '맨 이름' 은 못 가립니다.
_MASK_PII = r"""(opts) => {
  const ownName = (opts && opts.ownName) || "";
  const known = ((opts && opts.known) || []).filter((k) => k && k.length >= 2)
                .sort((a, b) => b.length - a.length);  // 긴 것부터: 부분치환 방지
  const reNameId = /[가-힣A-Za-z][가-힣A-Za-z ]{0,19}\s*\(\s*\d{4,10}\s*\)/g;
  const reEmail = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
  const rePhone = /0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{4}/g;
  const reId = /\b\d{7,10}\b/g;
  const mask = (s) => {
    if (!s) return s;
    s = s.replace(reNameId, "[이름]([ID])").replace(reEmail, "[이메일]")
         .replace(rePhone, "[전화]").replace(reId, "[ID]");
    if (ownName && ownName.length >= 2) s = s.split(ownName).join("[이름]");
    for (const k of known) s = s.split(k).join("[이름]");
    return s;
  };
  if (!document.body) return;
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    const p = n.parentNode && n.parentNode.nodeName;
    if (p === 'STYLE' || p === 'SCRIPT') continue;
    const v = mask(n.nodeValue);
    if (v !== n.nodeValue) n.nodeValue = v;
  }
  for (const el of document.querySelectorAll('input, textarea')) {
    const v = mask(el.value || '');
    if (v !== el.value) el.value = v;
  }
}"""


def path_for(md: Path) -> Path:
    """`글.md` 옆의 `글.png`."""
    return Path(md).with_suffix(".png")


def sender_pairs(raws: Iterable[str]) -> list[tuple[str, str]]:
    """원문 -> 이름표 치환 목록. 긴 것부터 바꿔야 부분 치환이 안 생깁니다.

    `김진욱 (101394)` 하나에서 세 쌍이 나옵니다 — 통째, 이름만, 사번만.
    상세 화면이 이름만 따로 보여 주는 경우가 있기 때문입니다.
    """
    pairs: dict[str, str] = {}
    for raw in raws:
        raw = (raw or "").strip()
        if not raw:
            continue
        label = anon.sender(raw)
        pairs[raw] = label
        m = anon._SENDER.match(raw)
        if m:
            name, ident = m.group("name").strip(), m.group("id").strip()
            if len(name) >= 2:
                pairs.setdefault(name, label)
            if len(ident) >= 4:
                pairs.setdefault(ident, label)
    return sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)


def anonymize_senders(page, known: Iterable[str] = ()) -> bool:
    """화면의 보낸이를 `.md` 와 같은 이름표로 바꿉니다. 다 바꿨으면 True.

    목록 행에서 읽은 보낸이(known)와, 화면에서 '이름 (사번)' 꼴로 보이는
    것을 전부 바꿉니다. 상세 아래에 목록이 또 붙어 있는 화면이 있어서
    다른 사람의 이름도 찍힐 수 있습니다.
    """
    found = list(page.evaluate(_FIND, SENDER_PATTERN) or [])
    pairs = sender_pairs([*known, *found])
    if pairs:
        page.evaluate(_REPLACE, [list(p) for p in pairs])
    text = page.evaluate(_VISIBLE_TEXT) or ""
    left = [a for a, _ in pairs if a in text]
    left += [m.group(0) for m in re.finditer(SENDER_PATTERN, text)]
    if left:
        log.warning("보낸이를 다 가리지 못해 스크린샷을 남기지 않습니다(%d곳).", len(left))
        return False
    return True


def mask_pii_dom(page, own_name: str = "", known_names: Iterable[str] = ()) -> None:
    """화면 글자/입력값 속 확실한 PII 토큰·본인 이름·아는 이름을 그 자리에서 가립니다.

    known_names 는 목록 행에서 '구조적으로' 뽑은 작성자 이름들입니다 —
    사번이 안 붙어 정규식으론 못 잡는 '맨 이름' 이라도, 어느 자리가 이름인지
    아는 경우엔 정확히 그 문자열만 치환합니다(본문 내용은 안 건드림).
    스크린샷 직전에만 부릅니다(라이브 DOM 을 바꿉니다). 실패해도 수집을
    막지 않습니다 — 스크린샷은 덤이기 때문입니다.
    """
    try:
        page.evaluate(_MASK_PII, {
            "ownName": (own_name or "").strip(),
            "known": [str(k).strip() for k in known_names if k and str(k).strip()],
        })
    except Exception as exc:
        log.debug("화면 PII 마스킹 실패: %s", exc)


def take(page, md: Path, *, senders: Iterable[str] | None = None,
         own_name: str = "", known_names: Iterable[str] = ()) -> Path | None:
    """`md` 옆에 전체 페이지 스크린샷을 남깁니다. 실패하면 None.

    senders 를 주면(쪽지) 보낸이를 안정적 이름표로 익명화한 뒤에 찍고,
    익명화를 끝내지 못하면 찍지 않습니다. 그 뒤(그리고 쪽지가 아닌
    화면에서도) 확실한 PII 토큰·본인 이름·목록에서 아는 작성자 이름을
    화면에서 가린 다음 찍습니다 — `.md`·`.html` 과 같은 기준으로 그림에도
    PII 가 남지 않게 합니다.
    """
    out = path_for(md)
    try:
        if senders is not None and not anonymize_senders(page, senders):
            return None
        mask_pii_dom(page, own_name, known_names)
        page.screenshot(
            path=str(out), full_page=True, type="png",
            mask=[page.locator(SELF_INFO)], animations="disabled",
        )
        return out
    except Exception as exc:
        log.warning("스크린샷 실패(%s): %s", Path(md).name, exc)
        return None
