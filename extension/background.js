/* gymhall 배경 서비스워커 — 콘텐츠 스크립트 → 네이티브 호스트(stdio).
 *
 * 내용은 네트워크를 타지 않습니다: connectNative 로 로컬 호스트에만 보냅니다.
 * 호스트가 게이트 통과 후 봉인 저장합니다(gymhall activity 파이프라인).
 */
const HOST = "com.gymhall.activity";
let port = null;

function connect() {
  try {
    port = chrome.runtime.connectNative(HOST);
    port.onDisconnect.addListener(() => { port = null; });
  } catch (e) {
    port = null;
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || msg.v !== 1) return;
  if (!port) connect();
  try {
    if (port) port.postMessage(msg);
  } catch (e) {
    port = null;
  }
});
