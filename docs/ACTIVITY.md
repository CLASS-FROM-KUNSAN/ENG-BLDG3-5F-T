# 활동 기록 — PDF 폼 필드 값 (설계 + 1차 구현)

녹음/녹화와 **같은 타임라인**에, PDF 폼에 입력한 값을 전사처럼 봉인 저장합니다.
**키 입력을 가로채지 않습니다** — 어댑터가 커밋된 "필드 값"만 읽습니다. 그래서
비밀번호 제외가 휴리스틱이 아니라 **스펙 신호**(`/Ff` 플래그 등)로 확정됩니다.

> **책임 고지.** 이 기능은 **당신 전용 기기에서만** 쓰세요. 기기를 공유하면 타인의
> 입력까지 담길 수 있습니다. 결과물은 **private 저장소에만** 두세요.

> **구현 상태.** 안전 척추(하드웨어/OS 게이트 + 정책 엔진)는 **구현됨**
> (`src/gymhall/activity/`, `python -m gymhall activity gate`). 저장소(SQLite 봉인)와
> 캡처 어댑터(A→C→B→D)는 **다음 증분**.

---

## 1. 전제

1. **본인·전용 기기·본인 활동만.** `machine_is_private = true`가 아니면 어댑터는 비활성.
2. **비밀번호 최소 수집.** 자격증명 필드는 담은 뒤 잠그는 게 아니라 **스펙 신호로 아예 제외**.
3. **기본 꺼짐 + 켜지면 보임 + 패닉 정지.**
4. **로컬·봉인. 업로드는 봉인된 것만. 평문 at-rest 없음.**

## 2. 안 하는 것 (버린 설계)
- **전역 키보드 후킹** — 안 만듭니다. 필터를 붙여도 후킹 지점이 전역이면 프로파일이 같습니다.
- **커밋된 입력에서 비밀번호를 정규식/알고리즘으로 찾아내기** — 자격증명 수집 로직이라 안 만듭니다.
- **커널 드라이버로 복구모드에서도 못 읽게 숨기기** — 안티포렌식/루트킷이라 안 만듭니다.

---

## 3. 하드웨어/OS 무결성 게이트 (구현됨, fail-closed)

봉인 저장은 이 게이트를 통과해야만 시작합니다. 하나라도 실패하면 저장 API 가
`GateError` 를 던지고, 메모리 버퍼는 폐기, **임시 평문 파일은 만들어지지 않습니다.**
`python -m gymhall activity gate` 로 지금 점검할 수 있습니다.

| 이유코드 | 검사(비관리자로 읽음) | 통과 조건 |
|---|---|---|
| `tpm_version` | `ACPI\MSFT0101`(TPM 2.0 HID) 존재 + `TPM\WMI` `TaskReadyForStorage` | TPM 2.0 이 저장 준비됨 |
| `secure_boot` | 레지스트리 `SecureBoot\State\UEFISecureBootEnabled` | `= 1` |
| `vbs_hvci` | `Win32_DeviceGuard` | VBS 실행 + HVCI 실행 |
| `elevated` | `IsUserAnAdmin()` | **관리자 아님**(최소 권한) |
| `debugger` | `IsDebuggerPresent()` | 디버거 미부착 |
| `attestation` | (옵션) EK 체인 + quote | `require_attestation=true` 면 **미구현→fail** |

**비관리자로 돈다**는 점이 중요합니다. `Get-Tpm`·`Confirm-SecureBootUEFI` 는 관리자
전용이라 "관리자 아님" 요구와 충돌해서, 레지스트리/PnP 등 **비관리자 신호**로 읽습니다.
(실기 Windows 11 에서 5개 전부 통과 확인.)

### 읽기/쓰기 비대칭 (기본: 대칭 차단)
- **쓰기(저장)**: 게이트 실패 → 하드 차단.
- **읽기(복호)**: 기본 `read_enforces_gate = true` → **읽기도 차단**. 게이트 실패는
  "전용·신뢰 기기" 전제가 깨졌다는 신호라, 그 위에서 복호해 평문을 오염 가능한 OS 에
  노출하지 않습니다. 영구 잠김을 피하려 **`--break-glass`** 로 한 번만 허용(로그 남김).
  `read_enforces_gate = false` 로 "경고 후 허용"으로 바꿀 수 있습니다.

### 정직한 한계 (숨기면 "납득"이 가짜)
- 게이트는 **정직하게 약한 기기**(TPM 2.0 없음·Secure Boot 꺼짐·관리자 실행·디버거)의
  저장을 확실히 막습니다. 그러나 **이미 커널/루트를 쥔 공격자**는 프로브 호출을 속일 수
  있습니다 — **방어심화**이지 '깨끗한 OS 의 암호학적 증명'이 아닙니다.
- **완전한 TPM 원격증명(EK 체인 + quote)은 미구현.** `require_attestation=true` 로 켜면
  거짓 통과 대신 **fail-closed**.

---

## 4. 정책 엔진 (구현됨) — SKIP 우선순위

어댑터가 커밋된 필드 값을 넘기면, 위에서부터 하나라도 '버리기'면 끝입니다
(fail-closed: 애매하면 버림).

1. **스코프** — 웹은 허용 오리진 밖이면 드롭, 데스크톱은 프로세스 스코프 없으면 드롭.
2. **비밀번호** — `/Ff` bit 13 · `type=password` · UIA `IsPassword` → 드롭(끌 수 없음).
3. **서명** — `/FT /Sig` → 드롭.
4. **리치텍스트** — `exclude_richtext` 면 `/Ff` bit 26 드롭.
5. **이름 블랙리스트** — `*ssn* *card* *cvv* *주민* *계좌* *password* *secret*` (억제 전용).
6. **담기** — 저장 전 레드액션(주민번호, Luhn 검증 카드번호). 완벽 보장 아님, 잔여 그물.

---

## 5. 캡처 어댑터 — 필드 값(키 후킹 0), 전용 기기만

| # | 어댑터 | 방법 | 스코프 | 비밀번호 제외 | 상태 |
|---|---|---|---|---|---|
| A | PDF.js | content script 가 필드 모델/`annotationStorage` 읽기 | 오리진+문서 | `/Ff` bit13 | 다음(1순위) |
| C | 데스크톱 Acrobat (JS) | 폴더 스크립트가 저장 시 필드 값 사이드카로 | 앱+파일 | `/Ff` bit13 | 다음 |
| B | Acrobat Web | 그 오리진에서 편집기 필드 모델 | 오리진+문서 | type=password | 이후 |
| D | 데스크톱 Acrobat (UIA) | 포그라운드 문서 창의 ValuePattern | 프로세스+창 | UIA IsPassword | 최후 폴백 |

- **Chrome/Edge 내장 뷰어는 정직하게 미지원** → "안전 뷰어로 열기"로 A 경로 유도.
- `keys_level`: `field_commit`(blur/change/Enter, 기본) · `document_save` · `field_debounce`.
  셋 다 **포커스된 필드 값만**, 전역 키 이벤트 안 받음.
- 전달: 네이티브 메시징(stdio)뿐 → 네트워크 안 탐. 호스트가 디스크 쓰기 전 봉인.
- 시계: 호스트가 세션 시계 주인, 확장이 `performance.now()`로 짝맞춤 → 녹화와 같은 축.

---

## 6. 저장·봉인
- 디스크 쓰기 **전에** AEAD + **공개키 봉투**. SQLite 엔 암호문 + 래핑 키 + 타임라인 메타만.
- 개인키 **TPM 추출 불가** + Windows Hello. 백업키 없음, 기기 죽으면 **복구 불가(수용)**.
- 업로드 `sealed_only` 또는 `off`. public 저장소 금지.

## 7. 가시성·동의 (어댑터 증분에서)
- 캡처 중엔 보임(탭 배지/트레이), 사이트별 옵트인, 시크릿창 차단, 패닉 일시정지.
- 첫 실행 모달·설정 화면·이 문서 3곳에 책임 고지.

---

## 8. 설정 `[activity]` (구현됨, 기본 꺼짐)

```toml
[activity]
enabled = false
machine_is_private = false      # 전용 기기 아니면 어댑터 비활성
follow_timetable = true
timetable_slots = []
keys_level = "field_commit"     # field_commit | document_save | field_debounce
debounce_ms = 800
origins = []                    # 웹 어댑터 허용 오리진
process_allowlist = ["Acrobat.exe"]
upload = "sealed_only"          # off | sealed_only
pause_hotkey = "Ctrl+Alt+P"

[activity.adapters]             # 안 쓰는 건 공격면이라 기본 꺼짐
pdfjs = false
acrobat_web = false
acrobat_js = false
acrobat_uia = false

[activity.policy]
drop_password = true            # 끌 수 없음(검증에서 강제)
exclude_signature = true
exclude_richtext = false
name_blacklist = ["*ssn*", "*card*", "*cvv*", "*주민*", "*계좌*", "*password*", "*secret*"]
redact_patterns = true

[activity.gate]
require_tpm2 = true
require_secure_boot = true
require_vbs_hvci = true
require_not_elevated = true
require_no_debugger = true
require_attestation = false     # 켜면 미구현이라 fail-closed
read_enforces_gate = true       # 대칭 차단. false 면 읽기는 경고 후 허용

[activity.store]
public_key = ""                 # 비면 저장 안 됨
key_storage = "tpm"
require_hello = true
recovery_key = false
retention_days = 0
```

## 9. 명령
```
python -m gymhall activity gate      이 기기가 봉인 저장을 허용받는지 점검
```
(캡처/저장 명령은 어댑터 증분에서 추가.)

## 10. 빌드 순서
스키마 → 정책 엔진(완료) → 게이트(완료) → **A(PDF.js)** → C(Acrobat JS) → B(Acrobat Web) → D(UIA).
각 어댑터는 `어댑터 → 테스트 → 설정/검증 → docs`. 한 번에 하나씩.
