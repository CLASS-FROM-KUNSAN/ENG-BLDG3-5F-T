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
- WASAPI 루프백 실제 캡처 (가짜 소스로만 확인)
- 작업 스케줄러 등록과 실행
- `.bat` / `.vbs` 의 실제 동작 (CI 는 문법과 CRLF 만 검사)
- faster-whisper 실제 전사 품질 (가짜 모델로 루프만 확인)
- eClass 실제 로그인과 수집 (익명화 픽스처로만 확인)
