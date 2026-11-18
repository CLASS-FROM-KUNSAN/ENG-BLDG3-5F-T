# gymhall 활동 기록 — 브라우저 확장 (웹 PDF 뷰어)

본인 PDF 웹 뷰어(`acrobat.adobe.com` 등)에서 채운 **폼 필드 값**을 읽어 로컬
봉인 저장소로 보냅니다. 키 입력을 가로채지 않고 필드 커밋 값만 봅니다.
자격증명은 DOM 신호로 제외합니다. 전용 기기·게이트·봉인은 Python 쪽에서 강제합니다
(`docs/ACTIVITY.md`).

```
manifest.json   MV3. acrobat.adobe.com / documentcloud.adobe.com 에만 주입(오리진 추가 가능)
content.js      폼 필드 스캔 + 자격증명 제외(검증된 순수 로직) + 라이브 전송
background.js   네이티브 메시징(connectNative)으로 로컬 호스트에 전달 — 네트워크 안 탐
```

## 검증 상태
- **자격증명 제외 로직은 실제 Chromium 에서 검증됨**(`tests/test_extension_content.py`,
  Playwright). password 타입·`current/new-password`·`one-time-code`·폼 전염·이름
  블랙리스트 → 전부 제외. jest/Node 없이 이 레포의 Playwright 로 돕니다.
- **acrobat.adobe.com 은 라이브 DOM 으로 검증됨.** 실제 로그인 세션의 PDF 폼에서
  확인: 폼 입력은 `input[data-testid="textInput"]`, 비번 필드는 `type=password`.
  확장은 그 폼 필드만 이름으로 담고, 비번은 제외, AI 질문칸(`QnAQuestionInput`)·
  페이지번호칸은 안 건드립니다(`content.js` 의 SITE_PROFILES 로 그 사이트는
  `data-testid="textInput"` 로 범위를 좁힘). 그 구조는 CI Playwright 테스트로 고정.
  (Adobe 가 UI 를 개편하면 선택자 보강이 필요할 수 있습니다 — 유지보수 비용.)

## 설치(개발자 모드)
1. 크롬/엣지 → `chrome://extensions` → 개발자 모드 켜기 → **압축해제된 확장 프로그램 로드**
   → 이 `extension/` 폴더 선택.
2. 네이티브 호스트 등록(확장 → 로컬 Python 다리) — 관리자 불필요:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_host.ps1 -ExtensionId <로드한_확장_ID>
   ```
   호스트 매니페스트(`extension\host\com.gymhall.activity.json`)와 HKCU 레지스트리
   키를 만듭니다(런처 = `scripts\activity_host.bat` → `python -m gymhall.activity.native_host`).
   지우려면 `-Remove`. **등록 로직은 실기에서 매니페스트·레지스트리 기록까지 검증했습니다**;
   브라우저↔호스트 실제 전송은 아래 수동 확인으로 봅니다.
3. `config.toml` 에서 `activity.enabled=true`, `machine_is_private=true`,
   `activity.adapters.acrobat_web=true`, `activity.origins` 에 뷰어 오리진 추가.
4. 브라우저 재시작.

## 수동 E2E 확인 (브라우저↔호스트 전송)
확장 로직·네이티브 호스트·정책·봉인 저장소는 **각각 자동 검증**됐습니다(Playwright·
pytest). 유일하게 자동화 안 된 건 Chrome 의 네이티브 메시징 전송 자체입니다. 한 번 확인:
1. 허용 오리진의 웹 PDF 뷰어에서 폼에 값을 입력(커밋).
2. `python -m gymhall activity export` → 그 값이 봉인되어 복호됨, 비밀번호 필드는 없음.

## 라이브 확인 (acrobat.adobe.com)
그 사이트에서 폼 PDF 를 연 뒤, 개발자도구 콘솔에 붙여 넣으면 "무엇을 담고 무엇을
제외할지"를 저장 없이 미리 봅니다(제외 로직만 실행):
```js
// content.js 를 붙여 넣은 뒤:
__gymhallScan()   // { captured:[...], skipped:[...] }
```
`captured` 가 비어 있으면 Adobe 가 폼을 표준 DOM 으로 노출하지 않는다는 뜻이고,
그 경우 선택자 보강이나 다른 접근이 필요합니다.
