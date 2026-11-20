/* syncnote 활동 기록 — 웹 뷰어 콘텐츠 스크립트 (A/B 어댑터)
 *
 * 본인 PDF 웹 뷰어(acrobat.adobe.com 등)에서 채운 폼 필드 값을 읽어 배경
 * 서비스워커 → 네이티브 호스트로 보냅니다. 키 입력은 가로채지 않고 필드
 * 커밋(change/blur) 값만 봅니다. 자격증명은 **DOM 신호로 제외**합니다.
 *
 * 분류 로직(classify/scan)은 chrome API 와 무관한 순수 함수라, Playwright 로
 * 실제 브라우저 DOM 에 대해 자동 검증합니다(window.__syncnoteScan). 라이브 전송은
 * 확장 컨텍스트(chrome.runtime 존재)에서만 붙습니다.
 *
 * 정직한 한계: acrobat.adobe.com 은 Adobe 의 독자 웹앱이라 필드 DOM 구조가
 * 공개 PDF.js 와 다를 수 있습니다. 아래 선택자/규칙은 표준 폼 요소 기준이고,
 * Adobe 가 폼을 비표준(canvas 등)으로 그리면 그 사이트에선 안 잡힐 수 있습니다 —
 * 이는 라이브로만 확인됩니다. 제외 로직 자체는 표준 DOM 에서 검증됩니다.
 */
(function () {
  "use strict";

  var CRED_AC = ["current-password", "new-password", "one-time-code"];
  var NAME_BLACKLIST =
    /(pwd|passwd|password|pass|pin|otp|cvv|cvc|secret|token|seed|ssn|주민|계좌|2fa|mfa|security.?code)/i;

  // 사이트별 "지정 폼 필드" 선택자. 실측(acrobat.adobe.com)에서 PDF 폼 입력은
  // input[data-testid="textInput"] 로, 비번 필드는 type=password 로 렌더됩니다.
  // 이렇게 좁히면 AI 질문칸(QnAQuestionInput)·페이지번호칸 등 비-폼 요소가 안 잡힙니다.
  var SITE_PROFILES = [
    { match: /(^|\.)acrobat\.adobe\.com$/i,
      fieldSelector: 'input[data-testid="textInput"], textarea[data-testid="textInput"]' },
    { match: /(^|\.)documentcloud\.adobe\.com$/i,
      fieldSelector: 'input[data-testid="textInput"], textarea[data-testid="textInput"]' },
  ];

  function siteSelector() {
    var host = String(window.__syncnoteProfileHost || location.hostname || "").toLowerCase();
    for (var i = 0; i < SITE_PROFILES.length; i++) {
      if (SITE_PROFILES[i].match.test(host)) return SITE_PROFILES[i].fieldSelector;
    }
    return null; // 알려진 사이트가 아니면 일반 폼 요소 스캔
  }

  function acToken(el) {
    var a = (el.getAttribute("autocomplete") || "").trim().toLowerCase();
    if (!a) return "";
    var parts = a.split(/\s+/);
    return parts[parts.length - 1];
  }

  function formHasPassword(el) {
    var form = el.form;
    if (!form) return false;
    if (form.querySelector('input[type="password"]')) return true;
    var withAc = form.querySelectorAll("[autocomplete]");
    for (var i = 0; i < withAc.length; i++) {
      if (CRED_AC.indexOf(acToken(withAc[i])) !== -1) return true;
    }
    return false;
  }

  function nameOf(el) {
    return el.getAttribute("name") || el.id || el.getAttribute("aria-label") || "";
  }

  function skip(reason, name) {
    return { capture: false, reason: reason, name: name, value: "" };
  }

  // 우선순위: password 타입 → 자격증명 autocomplete → 폼 전염 → 이름 블랙리스트 → 담기.
  function classify(el) {
    var tag = (el.tagName || "").toLowerCase();
    var type = (el.getAttribute("type") || "text").toLowerCase();
    var name = nameOf(el);
    if (tag === "input" && type === "password") return skip("password_field", name);
    if (CRED_AC.indexOf(acToken(el)) !== -1) return skip("password_field", name);
    if (formHasPassword(el)) return skip("form_has_password", name);
    if (NAME_BLACKLIST.test(name)) return skip("name_blacklist", name);
    var value = ("value" in el && el.value !== undefined) ? el.value : (el.textContent || "");
    return { capture: true, reason: "", name: name, value: value };
  }

  var SKIP_INPUT_TYPES = {
    button: 1, submit: 1, reset: 1, checkbox: 1, radio: 1,
    file: 1, image: 1, hidden: 1, range: 1, color: 1,
  };

  function candidates(doc) {
    // 알려진 사이트: 지정 폼 필드만(비-폼 입력 제외). classify 가 비번 등은 또 거릅니다.
    var profileSel = siteSelector();
    if (profileSel) {
      return Array.prototype.slice.call(doc.querySelectorAll(profileSel));
    }
    var out = [];
    var formEls = doc.querySelectorAll("input, textarea, select");
    for (var i = 0; i < formEls.length; i++) {
      var el = formEls[i];
      if (el.tagName.toLowerCase() === "input") {
        var t = (el.getAttribute("type") || "text").toLowerCase();
        if (SKIP_INPUT_TYPES[t]) continue;
      }
      out.push(el);
    }
    var ce = doc.querySelectorAll('[contenteditable=""], [contenteditable="true"]');
    for (var j = 0; j < ce.length; j++) out.push(ce[j]);
    return out;
  }

  function scan(doc) {
    var captured = [], skipped = [];
    var els = candidates(doc);
    for (var i = 0; i < els.length; i++) {
      var r = classify(els[i]);
      if (r.capture) captured.push({ name: r.name, value: r.value });
      else skipped.push({ name: r.name, reason: r.reason });
    }
    return { captured: captured, skipped: skipped };
  }

  // 자동 검증용(순수 로직).
  window.__syncnoteScan = function () { return scan(document); };
  window.__syncnoteClassify = classify;

  // 라이브 전송 — 확장 컨텍스트에서만.
  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
    var isField = function (el) {
      return el && (el.matches &&
        (el.matches("input, textarea, select") || el.isContentEditable));
    };
    var commit = function (el) {
      var r = classify(el);
      if (!r.capture) {
        chrome.runtime.sendMessage({ v: 1, suppressed: r.reason,
          scope: { origin: location.origin, field_path: r.name } });
        return;
      }
      if (!r.value) return;
      chrome.runtime.sendMessage({
        v: 1, adapter: "acrobat_web",
        scope: { origin: location.origin,
          doc_hash: document.title || location.pathname, field_path: r.name },
        field: { name: r.name, type: "text" },
        value: r.value, commit_reason: "change", ts: Date.now(),
      });
    };
    var onEvt = function (e) { if (isField(e.target)) commit(e.target); };
    document.addEventListener("change", onEvt, true);
    document.addEventListener("focusout", onEvt, true);
  }
})();
