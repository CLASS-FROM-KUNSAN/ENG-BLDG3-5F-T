# 테스트

```bash
pip install pytest numpy requests
python -m pytest
```

247개 테스트가 **네트워크, 오디오 장치, 학교 계정 없이** 전부 돌아갑니다.
GitHub API 는 가짜 세션으로, 오디오 장치는 가짜 소스로, eClass 는
익명화한 HTML 픽스처로 대체합니다.

선택적으로 더 도는 것들:

| 조건 | 추가로 도는 테스트 |
|---|---|
| `ffmpeg` 설치됨 | 실제 영상/오디오 합치기 (`test_mux.py`) |
| `playwright` + Chromium | 실제 브라우저로 로그인 폼 조작 (`test_eclass_browser.py`) |

없으면 자동으로 건너뜁니다.

---

## 왜 여러 파이썬 버전에서 돌리는가

`.github/workflows/tests.yml` 은 3.10 / 3.11 / 3.12 / 3.13 에서 전부 돌립니다.
불필요해 보이지만, **실제로 이 저장소에서 3.13 에서만 멀쩡한 버그가 났습니다.**

`threading.Thread` 는 3.12 까지 내부 메서드 `_stop()` 을 가지고 있었고
3.13 에서 없어졌습니다. Thread 하위 클래스에서 `self._stop = Event()` 라고
쓰면 그 메서드를 덮어쓰게 되어, `join()` 이

```
TypeError: 'Event' object is not callable
```

로 터집니다. 3.13 에서는 아무 일도 일어나지 않습니다.
개발 환경이 3.13 이면 멀쩡하고, 사용자 PC(대부분 3.11/3.12)에서만
**녹화가 매 세그먼트 끝에서 깨집니다.**

`test_recorder_audio.py::test_thread_subclasses_do_not_shadow_thread_internals`
가 이걸 버전과 무관하게 잡습니다. 현재 실행 중인 `Thread` 를 보고
판단하지 않고, 역사적인 메서드 이름 목록을 고정해 두고 비교하기 때문입니다.

---

## 개발 중에 실제로 잡힌 버그들

테스트를 쓰면서 나온 것들입니다. 전부 회귀 테스트로 고정해 뒀습니다.

### 조용한 데이터 손실

| 버그 | 증상 | 테스트 |
|---|---|---|
| 업로드 재시도가 소진된 파일 핸들을 재사용 | 500 한 번이면 **0바이트 에셋**이 올라가고 200 으로 성공 보고. 녹화본이 백업된 줄 알지만 비어 있음 | `test_retry_resends_the_whole_file` |
| 업로드 후 크기 검증 없음 | 위 상황을 알아챌 방법이 없었음 | `test_size_mismatch_is_an_error` |

### 녹화가 깨지는 것

| 버그 | 증상 | 테스트 |
|---|---|---|
| `Thread._stop` 이름 충돌 | 3.11/3.12 에서 세그먼트마다 `join()` 이 TypeError | `test_thread_subclasses_do_not_shadow_thread_internals` |
| `ffmpeg -nostdin` | 종료 신호 `q` 가 무시돼 매 세그먼트 20초 타임아웃 후 강제 종료, 끝부분 잘림 | Xvfb 종단 테스트 |
| `mux` 의 단일 삭제 플래그 | 전사용 WAV 를 남기면 영상 원본도 같이 남아 작업 폴더에 계속 쌓임 | `test_delete_flags_are_independent` |
| `TrackResult.seconds` 가 48000 하드코딩 | 44.1kHz 설정에서 길이가 8% 틀어짐 | `test_reported_seconds_uses_real_samplerate` |

### 기능이 아예 죽어 있던 것

| 버그 | 증상 | 테스트 |
|---|---|---|
| `RealtimeTranscriber` 가 `@dataclass` + `Thread` | `__hash__` 가 지워져 **생성 자체가** `TypeError: unhashable type`. 실시간 전사를 켜는 순간 터짐 | `test_can_be_constructed` |
| 꼬리 오디오 폐기 | 정지 신호 뒤 버퍼에 남은 마지막 몇 초가 전사되지 않음 | `test_tail_audio_is_not_discarded` |
| 단순 데시메이션 리샘플 | 48k→16k 를 `[::3]` 로 뽑아 에일리어싱. 자음이 뭉개짐 | `test_resample_integer_ratio_averages_not_decimates` |

### 폭주 / 무한 대기

| 버그 | 증상 | 테스트 |
|---|---|---|
| 감시 트리거 재시작 폭주 | ffmpeg 가 즉시 죽고 Zoom 이 켜져 있으면 **0.0초에 25회** 재시작하며 빈 폴더로 디스크를 채움 | `test_gives_up_instead_of_thrashing` |
| `wait_until_gone` 무한 대기 | 감지 조건이 항상 참이면 영원히 멈춤 | `test_wait_until_gone_has_a_ceiling` |

### 영어 강의 / 언어

| 버그 | 증상 | 테스트 |
|---|---|---|
| `language = "ko"` 전역 고정 | 영어 수업을 한글로 받아씀("디스 이즈 어 바이너리 트리"). 그 과목 전사본이 통째로 못 쓰게 됨 | `test_language_setting_reaches_the_model` |
| 환각 목록에 "thank you" / "bye" | 영어 수업에서 교수가 수업을 마치며 하는 실제 인사가 지워짐 | `test_short_english_utterances_are_kept` |
| `no_speech_prob` 단독 판정 | 0.6~0.85 구간에서 모델이 확신 없이 지어낸 문장을 놓침 | `test_logprob_rule_needs_both_signals_bad` |
| 후보 밖 언어 오판 | 한국어 강의의 영어 용어 구간이 일본어로 판정됨 | `test_wrong_language_is_corrected_to_best_candidate` |

### 참고 저장소 정독 후 찾은 것 (2차)

처음에 겉핥기로 넘긴 저장소들을 파일 단위로 다시 읽어 찾았습니다.

| 버그 | 증상 | 테스트 |
|---|---|---|
| GPU 선택이 VRAM 을 안 봄 | 2GB 내장 GPU 에서도 cuda 를 골라 모델 로딩 중 OOM | `test_small_vram_gpu_is_rejected` |
| `float16` 하드코딩 | 지원하지 않는 카드에서 로딩 실패 | `test_compute_type_is_asked_not_hardcoded` |
| 첨부 탐색이 프레임을 안 봄 | 본문이 iframe 안에 그려지면 첨부를 영영 못 찾음 | (프레임 순회 추가) |
| Actions 가 시크릿 없이 실패 | 설정 전까지 하루 3회 실패 알림이 쌓임 | (게이트 단계 추가) |
| 소요 시간이 지어낸 숫자 | "1시간에 10분" — 기기·부하에 따라 몇 배씩 다름 | `test_describe_says_whether_it_is_measured` |

### 기타

| 버그 | 증상 | 테스트 |
|---|---|---|
| `--dry-run` 이 GitHub API 호출 | 토큰 없이 미리보기 불가 | `test_dry_run_makes_no_network_calls` |
| 한글 파일명이 `1.pptx` 로 뭉개짐 | GitHub 가 비ASCII 를 버려 서로 다른 과목 자료가 충돌 | `test_korean_names_get_unique_hash` |
| 에셋 목록을 파일마다 조회 | 업로드 100개 = API 호출 200회 | `test_asset_list_is_fetched_once` |
| 로그인 선택자 `#userId` / `#password` | 실제 id 는 `#id` / `#pw`. name 속성과 다름 | `test_id_attributes_differ_from_name_attributes` |
| TOML 문법 오류가 생 예외로 노출 | 사용자가 가장 자주 내는 오류인데 스택트레이스만 나옴 | `test_toml_syntax_error_is_explained` |

---

## 픽스처

`tests/fixtures/*.html` 은 실제 eClass 페이지에서 **구조만** 뽑은 것입니다.

- `main_logged_out.html` — 로그인 화면. 개발자도구로 확인한 마크업 그대로
  (이 페이지에는 개인정보가 없습니다)
- `main_logged_in.html` — 과목 목록 `select`. 학번·이름·교수명은 전부 가짜
- `board_list.html` — 게시판 목록 테이블. 작성자는 "홍길동"

`test_fixtures_contain_no_personal_data` 가 학번 모양의 7자리 숫자와
이메일 모양 문자열이 섞여 들어가지 않는지 매번 검사합니다.

학교가 LMS 를 개편하면 이 픽스처를 갱신해야 합니다.
새 구조는 `scripts\eclass_probe.bat` 으로 뜰 수 있습니다.

---

## 테스트하지 못한 것

정직하게 적어 둡니다. 아래는 **Windows 실기에서 직접 확인해야 합니다.**

- `gdigrab` 화면 캡처 (Linux 에서는 `x11grab` 으로만 확인)
  - 2026-11-13 Windows 실기에서 **한 화면짜리 PC** 로는 확인했습니다:
    `monitor = 1` 이 2880×1800 을 어긋남 없이 잡았고(출력 1280×800,
    비율 일치), 배율 200% 에서도 좌표가 안 줄었습니다
    (`SetProcessDPIAware` 가 먹습니다).
  - **아직 확인 못 한 것: 모니터 두 대.** `monitor = 2` 가 그 화면만
    잡는지, 보조 모니터가 왼쪽일 때 `-offset_x` 가 음수로 나가는지는
    두 대 꽂은 PC 에서 다시 돌려야 합니다.
- WASAPI 루프백 실제 캡처 (가짜 소스로만 확인)
- 작업 스케줄러 등록과 실행
- `.bat` / `.vbs` 의 실제 동작 (CI 는 문법과 CRLF 만 검사)
- faster-whisper 실제 전사 품질 (가짜 모델로 루프만 확인)
- eClass 실제 로그인과 수집 (익명화 픽스처로만 확인)

---

## CI 초록불은 sync 잡의 성공을 뜻하지 않습니다

2026-11-12 실행에서 테스트 687개가 전부 통과했고 CI 는 초록이었습니다.
같은 날 `eclass sync` 잡은 **파일 16개 중 0개를 올렸고**, 학습목차는
16주 과목 아홉 개가 전부 3주차에서 끊겼고, 게시글 첨부 27건이
실패했습니다. 회귀 세 건이 전부 초록불 밑에 있었습니다.

테스트를 더 쓰면 되는 문제가 아니었습니다. **실행 로그가 최종 진실인데
그걸 안 본 것**입니다. 테스트는 제가 상상한 상황만 검사하고, sync 잡은
실제 서버를 상대합니다.

규칙 두 가지:

1. **결정적 실패는 `::error::` 로 올립니다.** `log.warning` 은 초록불
   옆에 작게 뜨고 아무도 안 봅니다. "업로드 0건" 같은 것은
   `cli._actions_error()` 로 빨간 줄을 띄우고 **종료 코드 1** 을
   돌려줘야 합니다. 2026-11-12 사고는 이것 하나로 잡혔을 것입니다 —
   '업로드 완료 0건' 이 로그에 그대로 있었는데 종료 코드가 0 이었습니다.

2. **사전 점검을 없애면 사후 검증을 넣습니다.** `check_access` 의
   push 권한 차단을 경고로 낮춘 것은 옳았습니다(Actions 토큰은
   `contents: write` 가 있어도 `/repos/` 응답의 `permissions.push` 가
   false 로 옵니다). 하지만 막는 것을 치웠으면 끝에서 세는 것이
   반드시 있어야 합니다. 지금은 `upload_many(tally=...)` 가
   성공/중복/실패/이름충돌/파일없음을 따로 셉니다 — 그래야 "전부
   중복이라 0건" 과 "전부 실패해서 0건" 이 구분됩니다.

### 로컬에서 재현되지 않는 버그

`_enter_detail` 의 `expect_navigation` 이 그런 경우입니다. 127.0.0.1
폼 POST 는 1ms 안에 끝나서, 기다리든 말든 로컬에서는 둘 다 초록입니다.
실제 서버에서만 틈이 벌어집니다.

**테스트를 제안할 때 머릿속으로 변이를 한 번 돌리세요.**
"이 테스트가 그 코드를 삭제했을 때 실패하는가?" 를 먼저 자문합니다.
실패하지 않으면 그건 테스트가 아니라 **위장**입니다.

실제로 V9(`SetProcessDPIAware`)의 Windows 통합 테스트 제안이 이
함정이었습니다. 테스트 프로세스에서 `SetProcessDpiAwareness(2)` 를
부른 뒤 `GetSystemMetrics` 와 `devices` 출력을 비교하자는 것이었는데,
그러면 양쪽 다 DPI-aware 라 그 호출을 지워도 같은 값이 나옵니다.
CI 러너는 배율 100% 라 더더욱 차이가 없습니다. 넣었으면 "방어선이
생겼다" 는 착각만 남았을 것입니다.

이런 것은 **동작 대신 수단을 고정합니다.** 소스에 `expect_navigation`
이 있는지 직접 검사하는 테스트가
`tests/test_eclass_detail_entry.py::test_enter_detail_actually_waits_for_navigation`
입니다. 못생겼지만, 동작으로 못 잡는 것을 잡습니다.


---

## wiring 테스트는 실물 객체에 묶습니다

이 코드베이스에서 "테스트가 전부 초록인데 기능은 죽어 있다" 가 두 번
나왔습니다. 둘 다 구조가 같습니다.

| 사례 | 테스트가 확인한 것 | 실제로 죽은 것 |
|---|---|---|
| `page.evaluate` 인자 | 로컬에서는 인자를 넘겨도 동작 | 러너의 playwright 에서 `refs.set` 오류 |
| `_silence_verdict` | `FakeRecorder` 에 `.results` 가 있음 | 진짜 `AudioRecorder` 에 그 속성이 없음 |

**테스트 대역이 실물과 다른 인터페이스를 가져도, 테스트는 대역에 맞춰
쓰이므로 초록입니다.** 그리고 `getattr(obj, "attr", default)` 같은
관대한 기본값이 그 어긋남을 조용히 삼킵니다 — 예외도 안 납니다.

규칙:

1. `Fake*` 는 **로직** 검증용입니다. "조용한 트랙이 전부일 때만 멈춘다"
   같은 판단 규칙은 대역으로 짜도 됩니다.
2. **`getattr(obj, "name", default)` 를 쓴 곳에는 반드시 실물 객체에
   묶은 테스트를 붙입니다.** 그 기본값이 실패를 삼키는 지점입니다.
3. 설정값이 두 단계 이상 넘어가면(`cfg → Recorder → Track`) 끝까지
   도착하는지 따로 봅니다. 중간에서 멈춰도 아무도 안 터집니다.
   `test_silence_peak_reaches_the_track` 이 그 예입니다.
4. **로그 문구를 검사할 거면 그 로그를 내는 코드를 실제로 돌리세요.**
   테스트 안에서 같은 문구를 다시 `log.warning(...)` 하면, 프로덕션에서
   그 줄을 통째로 지워도 초록입니다.

---

## OS 분기는 순수 로직으로 떼고, OS 판정은 호출부에서만

위 규칙으로도 안 잡히는 것이 하나 더 있었습니다. **실물 객체에
묶어도 실행 환경이 다르면 그 분기를 아예 안 탑니다.**

모니터 선택이 그랬습니다:

```python
def _input_args(self):
    if os.name != "nt":
        return [... x11grab ...]     # ← 리눅스 CI 는 여기서 끝
    if self.backend == "ddagrab":
        ...                          # ← 한 줄도 안 돕니다
```

Windows 분기 전체가 CI 에서 실행조차 되지 않습니다. 테스트를 아무리
잘 짜도 그 코드에 도달하지 못합니다.

**OS 가 결정하는 부분과 로직을 분리합니다.** 로직 쪽은 필요한 것을
인자로 받습니다:

```python
def _input_args(self):                       # OS 판정은 여기서만
    if os.name != "nt":
        return [... x11grab ...]
    return self.win_input_args(enumerate_monitors())

def win_input_args(self, monitors):          # 어느 OS 에서나 시험 가능
    ...
```

`tests/test_video_target.py` 가 `win_input_args([...])` 를 직접
부릅니다 — monkeypatch 도 필요 없습니다.

### 그 한 줄짜리 배선도 테스트합니다

`win_input_args` 는 CI 에서 잘 돕니다. 그런데 `_input_args` 가
Windows 에서 `enumerate_monitors()` 를 불러 **그걸 넘기는지** 는
여전히 안 돕니다. 한 줄이지만 거기서 인자를 빠뜨리면 `monitor` 설정이
통째로 죽고, 테스트는 초록이고, 사용자만 압니다.

프로덕션 서명에 `_platform=` / `_monitors=` 같은 테스트 전용 인자를
넣는 방법도 있지만, **`os.name` 을 바꿔 끼우는 쪽이 낫습니다.**
`_input_args` 가 그 값을 호출 시점에 읽으므로 진짜 함수가 진짜
분기를 탑니다:

```python
monkeypatch.setattr(os, "name", "nt")
monkeypatch.setattr(video, "enumerate_monitors", lambda: [...])
args = VideoRecorder(..., monitor=2)._input_args()
```

### 실패하면 기능만 포기하고 작업은 계속

모니터 목록을 못 읽는 것이 **녹화 실패**가 되면 안 됩니다. 전체
화면이 찍히는 것이 아무것도 안 찍히는 것보다 낫습니다.
`enumerate_monitors()` 는 어떤 예외든 삼키고 빈 리스트를 돌려주고,
빈 리스트는 "화면이 없다" 가 아니라 "모르겠다" 이므로 경고하지
않습니다(리눅스 개발 환경에서 매 실행 뜨면 안 됩니다).

`tests/test_silence.py` 아래쪽의 "가짜가 아니라 진짜 AudioRecorder 와
맞물리는지" 묶음이 이 규칙을 따른 것입니다.


---

## Windows 실기 메모

- **`python` 이 아니라 `py` 입니다.** 이 PC 들에서 `python` 은
  `C:\Windows\system32\python` 으로 잡히는데 진짜 인터프리터가
  아니라 아무 출력 없이 끝납니다. Microsoft Store 스텁이거나 PATH
  오염입니다. 문서의 `python -m gymhall …` 은 그 PC 에서 조용히
  실패하므로 `py -m gymhall …` 로 읽으세요.
- **`--dry-run` 은 녹화를 합니다.** 업로드만 흉내 냅니다. 전사
  대기열에도 실제로 들어갑니다. 2026-11-13 에 이것 때문에 무음
  테스트 파일 한 건이 대기열에 남았습니다.
- **기본 마이크를 믿지 마세요.** 그 PC 의 기본 마이크는
  `마이크 배열 (Audio Share)` 라는 가상 장치였고, 전 구간 0 이
  나왔습니다. 이제 로그가 **실제로 열린 장치 이름**을 적습니다.
- **"조용했다" 와 "아무것도 안 왔다" 는 다릅니다.** 그 녹음 파일을
  뜯어 보니 두 트랙 모두 11.8MB 에 0 아닌 바이트가 **하나도** 없었습니다
  (`volumedetect` 로 -91.0 dB = 16비트 바닥값). 진짜 마이크는 조용한
  방에서도 ADC 열잡음으로 ±1 LSB 는 흔들립니다. 정확히 0 이면 조용한
  게 아니라 신호 경로가 없는 것입니다. 반대로 시스템 소리는 WASAPI
  루프백이라 재생 중인 게 없으면 정확히 0 이 정상입니다. 그래서
  마이크일 때만 따로 오류를 냅니다.


---

## 인코딩은 로캘에 맡기지 않습니다

`open()` 과 `subprocess(text=True)` 는 인코딩을 안 적으면 **로캘**을
씁니다. 리눅스 CI 는 UTF-8 이라 늘 초록이고, 한글 Windows 는 cp949 라
거기서만 죽습니다. 2026-11-13 실기에서 테스트 2건이 이걸로 실패했습니다.

제품 코드는 멀쩡했습니다 — `ffmpeg_util.run` 과 `watcher` 가 이미
`encoding="utf-8", errors="replace"` 를 적고 있었습니다. 빠진 곳은
테스트였고(`tests/test_mux.py` 의 ffprobe 호출), **테스트가 못 도는
것도 고장입니다.** 그 PC 에서는 아무것도 검증되지 않습니다.

`tests/test_encoding_guard.py` 가 `src/` 와 `tests/` 의 모든 .py 를
AST 로 훑어 인코딩 없는 호출을 찾습니다. ruff 의 `PLW1514` 는
preview 전용인 데다 `subprocess(text=True)` 는 안 봅니다.

검사기를 쓰면서 **그 검사기가 두 번 틀렸습니다.** 둘 다 멀쩡한 코드를
위반으로 찍는 쪽이었습니다:

- `Path.open("rb")` 는 모드가 **첫** 인자인데 `open(path, "rb")` 처럼
  두 번째만 봤습니다 → 정상 바이너리 호출 6개를 전부 위반으로 찍음
- `read_text("utf-8")` 처럼 인코딩을 **위치 인자**로 넘긴 것을 못 봤습니다

그래서 검사기에는 자기검증 테스트 두 개를 붙였습니다 — 위반을 실제로
잡는지(`test_the_guard_actually_catches_things`)와 멀쩡한 것을 안
찍는지(`test_the_guard_does_not_cry_wolf`). 검사기가 조용히 아무것도
못 잡으면 전체 검사가 영원히 초록입니다.


---

## 부품이 아니라 **연결**을 검사합니다

변이 테스트를 돌려 보니 가장 아픈 결과가 나왔습니다. **각 PR 이
고치려던 바로 그 사고가 재발해도 테스트가 전부 초록**이었습니다.

| 변이 | 재발하는 사고 |
|---|---|
| 녹화 루프에서 `_wait_for_video_start()` 호출만 삭제 | pre-roll 2분 동안 남의 화면이 찍힘 |
| 시간표가 화면 시작을 pre-roll 만큼 당김 | 같은 사고, 다른 경로 |
| `_run_recording` 이 `video_not_before` 를 세션에 안 넘김 | 같은 사고, 또 다른 경로 |
| `eclass sync` 가 `_upload_eclass` 의 종료 코드를 버림 | 업로드 0건인데 CI 초록 |
| `_open_post` 가 `entered` 를 안 넘김 | 26건을 "링크를 못 찾았다" 로 오진 |

부품은 전부 잘 검사하고 있었습니다. `_wait_for_video_start` 도,
`_upload_eclass` 도, `_finish_post` 도 각각 테스트가 있습니다.
**그 부품을 잇는 자리가 끊겨도 아무도 안 터졌습니다.**

### 원칙 4 — 살아남은 변이를 "동치" 로 결론 내리기 전에

변이가 살아남으면 셋 중 하나입니다. 셋을 구분하지 않으면
`_silence_verdict` 가 죽어 있던 것과 같은 일이 또 납니다.

| 그 줄의 커버리지 | 뜻 | 할 일 |
|---|---|---|
| 한 번도 실행 안 됨 | **테스트 없음** (동치가 아닙니다) | 테스트 추가 |
| 실행됐는데 통과 | 어서션이 약하거나 진짜 동치 | 부작용을 검사하도록 교체 |
| 다른 테스트가 실패 | 그 테스트가 잡음 | 그대로 둠 |

**커버리지 0% 는 "동치" 가 아니라 "테스트 없음" 입니다.**
살아남은 변이는 PR 본문에 "왜 넘겼는지" 한 줄로 남깁니다.

### 원칙 5 — 로그가 아니라 부작용을 검사합니다

`_finish_post` 의 오류 문구를 검사하는 테스트는 있었습니다. 그런데
`_open_post` 가 `entered` 를 **안 넘겨도** 그 테스트는 통과합니다.
`_finish_post` 를 직접 불렀기 때문입니다.

결정의 **결과**를 어서션하세요. 첨부를 시도하지 않았는지, 종료 코드가
1 인지, 글이 미완료로 남았는지. 로그 문구만 보면 코드가 값을 통째로
무시해도 초록입니다.

### seam 은 한 군데가 아닙니다

`_run_recording` → `_run_recording_locked` → `RecordingSession` 처럼
단계가 둘이면 seam 도 둘입니다. 처음에 `_run_recording_locked` 를
직접 불러 테스트를 썼더니, **바깥쪽 seam 의 변이가 그대로
살아남았습니다.** 가장 바깥에서 부르세요.

### 원칙 6 — 고친 버그가 재발하면 빨개지는 테스트를 씁니다

`_run_recording` 이 값을 안 넘기던 버그를 고쳤다면, 테스트는
`RecordingSession(video_not_before=X)` 가 아니라
`_run_recording(video_not_before=X)` → **세션에 무엇이 들어갔나** 를
봐야 합니다. `tests/test_recording_seams.py` 가 그 묶음입니다.

### 원칙 7 — 버그의 "모양" 으로 전 코드를 훑습니다

변이 세트를 "최근 PR 변경분" 으로 잡았더니, **같은 모양이 다른 곳에
그대로 남아 있었습니다.** `cmd_upload` 가 `_upload_eclass` 와 똑같이
"전부 실패해도 `return 0`" 이었는데, 변경분이 아니라 기존 코드라
범위 밖이었습니다.

**변이 세트의 단위는 "PR 변경분" 이 아니라 "같은 실패 패턴" 입니다.**
버그를 하나 찾으면 그 모양으로 전 코드를 grep 하세요.

`rg "return 0" src/gymhall/cli.py` 로 훑어 나온 것 셋:

| 자리 | 조용한 0 |
|---|---|
| `cmd_transcribe_queue` | 큐 20건을 **전부 실패**해도 `run_queue` 가 `[]` 를 돌려주고 종료 코드 0. 야간 무인 작업이 그대로 성공으로 기록됩니다. |
| `cmd_transcribe_queue` (업로드) | 전사 결과 업로드에 tally 가 없었습니다. 전사는 됐는데 결과가 하나도 안 올라가도 0. |
| `cmd_record_start` | 다른 녹화가 돌고 있으면 `_run_recording` 이 `None` 을 돌려주는데, 그걸 버리고 0. **한 프레임도 안 찍고 "성공"** 입니다. |

셋 다 `upload_many` 와 같은 처방(tally + `::error::` + 종료 코드 1)을
했고, `tests/test_silent_zero.py` 가 **그 grep 자체를 고정**합니다 —
성공한 것만 돌려주는 함수(`upload_many`, `run_queue`)를 `tally` 없이
부르면 빨개집니다.

---

## 유닛 테스트로 잡을 수 없는 것

정직하게 목록으로 둡니다. 여기 있는 것을 "변이가 살아남았으니 동치"
로 넘기면, 다음 사람이 그 코드를 지웁니다.

| 코드 | 왜 못 잡나 | 어떻게 확인하나 |
|---|---|---|
| `monitors._win_monitors` 의 `SetProcessDPIAware()` | 고DPI Windows 에서만 좌표가 달라집니다. 호출을 지워도 어떤 유닛 테스트도 안 빨개집니다. CI 러너는 배율 100% 라 Windows CI 로도 못 잡습니다 — DPI-aware 여부가 결과에 영향이 없어 둘 다 통과합니다. | 배율 200% PC 에서 `devices` 출력이 실제 해상도(예: 2880×1800)와 같은지. **2026-11-13 수동 확인 완료.** |
| `video.win_input_args` 의 ddagrab `output_idx` | `primary_first()` 는 "주 모니터 먼저" 로 번호를 다시 매기고, ddagrab 의 `output_idx` 는 그래픽 드라이버가 정한 D3D11 출력 순서입니다. 둘이 같다는 근거가 없습니다. **모니터가 하나면 둘 다 0 이라 차이가 안 납니다** — 1대 PC 에서는 원리적으로 검증 불가. 지금 테스트는 "번호 − 1" 이라는 가정을 그대로 확인할 뿐입니다. | **모니터 2대 PC 에서 `backend = "ddagrab"`, `monitor = 2` 로 실측.** 그때까지 `ddagrab` + `monitor > 0` + 화면 2대 이상이면 런타임에 경고를 찍습니다. 기본값 `gdigrab` 은 좌표로 잘라내므로 이 문제가 없습니다. |

`_win_monitors` 의 나머지(EnumDisplayMonitors 호출, MONITORINFO 해석)도
같은 부류지만, 정렬·번호 매기기는 `primary_first()` 로 빼서 어느
OS 에서나 시험합니다. 남은 것은 **WinAPI 를 부르는 줄 자체**뿐입니다.


---

## 변이 테스트를 돌리는 동안에는 그 체크아웃을 건드리지 마세요

변이 러너는 소스를 바꿨다가 되돌립니다. 그동안 같은 체크아웃에서

- **전체 스위트를 돌리면** 변이된 소스를 검사합니다. 결과가 거짓입니다.
- **소스를 편집하면** 러너의 복원이 그 편집을 **덮어씁니다.**

둘 다 실제로 당했습니다. 두 번째가 더 고약합니다 — 테스트는 빨개지는데
소스를 보면 고친 내용이 사라져 있어서, 고친 적이 없는 것처럼 보입니다.

별도 worktree 를 쓰거나, 끝날 때까지 기다리세요.

```bash
git worktree add /tmp/mut HEAD
```

### 복원이 .pyc 때문에 안 먹는 경우

크기가 같은 변이(`return 1` → `return 0`)를 **같은 초 안에** 되돌리면,
파이썬이 묵은 `.pyc` 를 최신으로 착각해 변이된 코드를 계속 씁니다.
파일 수정 시각이 초 단위라서 생깁니다.

러너는 매 실행 전에 `__pycache__` 를 지우고 `PYTHONDONTWRITEBYTECODE=1`
로 pytest 를 띄워야 합니다. 둘 중 하나만 하면 부족합니다.
