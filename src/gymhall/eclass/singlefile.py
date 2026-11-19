"""페이지를 '한 파일짜리' 자체완결 HTML 로 캡처(SingleFile/Monolith 방식).

렌더링된 현재 DOM(동적으로 채워진 것 포함)을 그대로 직렬화하고, 외부
리소스(이미지·CSS·폰트)를 data URI 로 인라인해 오프라인에서도 같은 모습이
되게 합니다. 과제/첨부 '파일 다운로드' 와는 별개 기능으로, 페이지를 통째로
한 장씩 남기는 용도입니다.

**라이브 DOM 을 건드리지 않습니다** — document 를 깊은 복제한 사본에서만
작업합니다. 그래야 스냅샷 뒤에 같은 페이지를 다시 읽는 흐름(강의목록 진단
등)이 깨지지 않습니다.

프라이버시: 개인정보(이름(사번)·이메일·전화·학번/사번·본인 이름)는 **data URI
를 만들기 전에 텍스트 노드·일부 속성에서만** 가립니다. 그래야 base64 안의
숫자열이 학번으로 오인돼 이미지가 깨지지 않습니다. 플레이스홀더는 각괄호(<>)
대신 대괄호([ ])를 씁니다(outerHTML 로 직렬화하면 '<ID>' 가 이스케이프되거나
브라우저가 알 수 없는 태그로 보고 숨겨 버립니다).

한계(정직하게):
- 다른 출처(CDN)의 폰트·이미지는 CORS 로 못 가져올 수 있고, 그때는 원래
  링크를 둡니다(오프라인에서 그 리소스만 안 보일 수 있음).
- 스크립트는 **제거**합니다(다시 열 때 원본 JS 가 화면을 망가뜨리지 않도록).
  따라서 JS 로만 동작하던 상호작용(WebGL 등)은 보존되지 않습니다.
- 본문 자유텍스트 속 **제3자의 '맨 이름'** 은 자동 검출하지 못합니다(이름(사번)
  꼴·본인 이름만 가림). 민감하면 결과물을 로컬에만 두세요.
- 매우 큰 리소스(기본 3MB 초과)는 인라인하지 않고 건너뜁니다.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..logging_setup import get as get_logger

log = get_logger("eclass.singlefile")

# 페이지 안에서 도는 직렬화기. document 를 복제한 사본(root)에서만 작업하므로
# 라이브 페이지는 그대로입니다. async 라서 fetch 로 리소스를 가져옵니다(같은
# 출처 요청은 세션 쿠키로 나가 로그인 상태 자료를 담습니다).
_SERIALIZER = r"""
async (opts) => {
  const MAX = opts.maxBytes || 3000000;
  const ownName = opts.ownName || "";
  // 구조적으로 아는 이름들(작성자·이름 열·보낸이). 긴 것부터 치환해 부분
  // 치환을 막습니다. 사번 없는 '맨 이름' 도 여기 있으면 가립니다.
  const known = (opts.known || []).filter((k) => k && k.length >= 2)
                  .sort((a, b) => b.length - a.length);
  const root = document.documentElement.cloneNode(true);  // 라이브 DOM 불변

  // --- 1) 개인정보 마스킹(텍스트 노드 + 안전한 속성). data URI 생성 전에. ---
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
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    const p = n.parentNode && n.parentNode.nodeName;
    if (p === "STYLE" || p === "SCRIPT") continue;  // CSS/코드 텍스트는 보존
    const v = mask(n.nodeValue);
    if (v !== n.nodeValue) n.nodeValue = v;
  }
  for (const el of root.querySelectorAll("[title],[alt],[value],[aria-label],[placeholder]")) {
    for (const a of ["title", "alt", "value", "aria-label", "placeholder"]) {
      if (el.hasAttribute(a)) {
        const v = mask(el.getAttribute(a));
        if (v !== el.getAttribute(a)) el.setAttribute(a, v);
      }
    }
  }
  // 자격증명이 담길 수 있는 폼 값은 통째로 비웁니다.
  for (const inp of root.querySelectorAll("input")) {
    const t = (inp.getAttribute("type") || "text").toLowerCase();
    if (t === "password" || t === "hidden") inp.setAttribute("value", "");
  }

  // --- 2) 리소스를 data URI 로 ---
  const cache = new Map();
  const toDataURL = async (url) => {
    if (!url || url.startsWith("data:") || url.startsWith("blob:")) return null;
    if (cache.has(url)) return cache.get(url);
    let out = null;
    try {
      const r = await fetch(url, { credentials: "include" });
      if (r.ok) {
        const blob = await r.blob();
        if (blob.size <= MAX) {
          out = await new Promise((res) => {
            const fr = new FileReader();
            fr.onload = () => res(fr.result);
            fr.onerror = () => res(null);
            fr.readAsDataURL(blob);
          });
        }
      }
    } catch (e) { out = null; }
    cache.set(url, out);
    return out;
  };

  // 이미지(복제본의 src 속성 기준으로 절대화)
  for (const img of root.querySelectorAll("img")) {
    const u = img.getAttribute("src");
    img.removeAttribute("srcset");
    if (u) {
      let abs; try { abs = new URL(u, location.href).href; } catch (e) { abs = null; }
      if (abs) { const d = await toDataURL(abs); if (d) img.setAttribute("src", d); }
    }
  }

  // CSS url(...) 인라인(배경 이미지·폰트). base 기준으로 절대화.
  const inlineCssUrls = async (css, base) => {
    const urls = [];
    css.replace(/url\(\s*['"]?([^'")]+)['"]?\s*\)/g, (m, u) => { urls.push(u); return m; });
    for (const u of urls) {
      if (u.startsWith("data:")) continue;
      let abs; try { abs = new URL(u, base).href; } catch (e) { continue; }
      const d = await toDataURL(abs);
      if (d) css = css.split(u).join(d);
    }
    return css;
  };

  // 외부 스타일시트 -> <style>
  for (const link of [...root.querySelectorAll('link[rel~="stylesheet"][href]')]) {
    try {
      const href = new URL(link.getAttribute("href"), location.href).href;
      const r = await fetch(href, { credentials: "include" });
      if (!r.ok) continue;
      let css = await r.text();
      css = await inlineCssUrls(css, href);
      const style = document.createElement("style");
      style.setAttribute("data-inlined-from", href);
      style.textContent = css;
      link.replaceWith(style);
    } catch (e) { /* 실패 시 원래 link 유지 */ }
  }

  // 기존 <style> 안의 url() 도 인라인
  for (const style of root.querySelectorAll("style")) {
    try {
      const css = await inlineCssUrls(style.textContent || "", location.href);
      if (css !== style.textContent) style.textContent = css;
    } catch (e) {}
  }

  // --- 3) 스크립트 제거 + on* 핸들러 제거(다시 열 때 원격 호출 차단) ---
  root.querySelectorAll("script").forEach((s) => s.remove());
  for (const el of root.querySelectorAll("*")) {
    for (const attr of [...el.attributes]) {
      if (/^on/i.test(attr.name)) el.removeAttribute(attr.name);
    }
  }

  // --- 4) 상대경로가 깨지지 않게 <base> ---
  if (!root.querySelector("base")) {
    const b = document.createElement("base");
    b.setAttribute("href", location.href);
    const head = root.querySelector("head") || root;
    head.prepend(b);
  }

  return "<!DOCTYPE html>\n" + root.outerHTML;
}
"""


def capture(page, *, own_names: Iterable[str] = (), max_bytes_per_asset: int = 3_000_000,
            timeout_ms: int = 20000) -> str:
    """현재 페이지를 자체완결 HTML 문자열로. 라이브 DOM 은 건드리지 않습니다.

    실패하면 RuntimeError 를 올립니다(호출부가 수집을 계속하도록 잡습니다).
    """
    names = [str(n).strip() for n in own_names if n and len(str(n).strip()) >= 2]
    own = names[0] if names else ""
    try:
        page.set_default_timeout(timeout_ms)
    except Exception:
        pass
    html = page.evaluate(_SERIALIZER, {
        "maxBytes": int(max_bytes_per_asset),
        "ownName": own,
        "known": names,
    })
    if not isinstance(html, str) or len(html) < 50:
        raise RuntimeError("직렬화 결과가 비정상입니다")
    return html
