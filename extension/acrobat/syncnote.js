/* syncnote 활동 기록 — Acrobat 폴더 레벨 생산자 (C 어댑터)
 *
 * 설치(사용자 폴더, 관리자 불필요):
 *   %APPDATA%\Adobe\Acrobat\DC\JavaScripts\syncnote.js
 * 둔 뒤 Acrobat 을 완전히 재시작하면 로드됩니다.
 *
 * 하는 일: 문서의 폼 필드 중 **비밀번호 필드(Field.password === true)를 제외**한
 * 텍스트/선택 필드 값을 exportAsText 로 스테이징 폴더에 떨굽니다. Python 워처
 * (`syncnote activity watch-acrobat`)가 읽어 봉인한 뒤 사이드카를 즉시 삭제합니다.
 * 키 입력은 가로채지 않습니다 — 값은 이미 폼에 커밋된 것입니다.
 *
 * ── 실기 검증 결과(2026-11): 미검증 / Reader 로는 불가 ──────────────────────
 * 이 생산자는 **무료 Acrobat Reader 에서 동작하지 않습니다.** 실기 테스트에서
 * 폴더 레벨 trustedFunction 호출 시 Reader 가 **충돌**했습니다(폴더 레벨 권한
 * 스크립트는 Acrobat Pro 기능). 또한 Reader 는 OLE 자동화를 노출하지 않아 자동
 * 검증도 불가합니다. 즉 **C 어댑터는 Acrobat Pro 가 필요**합니다. 이 파일은 Pro
 * 환경에서 검증될 때까지 '초안(unverified)' 입니다 — 설치해 쓰지 마세요.
 * (주 경로는 웹 뷰어용 브라우저 확장입니다: 상위 extension/ 참고.)
 */

var SYNCNOTE_STAGING = "/C/Users/seong/AppData/Local/syncnote_activity/.staging/";

// eslint-disable-next-line no-unused-vars
var syncnoteExport = app.trustedFunction(function (doc) {
  app.beginPriv();
  try {
    var fields = [];
    for (var i = 0; i < doc.numFields; i++) {
      var nm = doc.getNthFieldName(i);
      var f = doc.getField(nm);
      if (!f) continue;
      if (f.type !== "text" && f.type !== "combobox" && f.type !== "listbox") continue;
      if (f.password === true) continue;        // 비밀번호 필드 제외(스펙 신호)
      fields.push(nm);
    }
    if (fields.length === 0) { app.endPriv(); return "nofields"; }
    var path = SYNCNOTE_STAGING + "acrobat_" + (new Date().getTime()) + ".txt";
    doc.exportAsText({ cPath: path, aFields: fields });
    app.endPriv();
    return "ok:" + path;
  } catch (e) {
    app.endPriv();
    return "error:" + e;
  }
});
