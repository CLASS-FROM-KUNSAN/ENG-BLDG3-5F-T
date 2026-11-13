# 설정 안내

전체 항목과 주석은 [`config.example.toml`](../config.example.toml) 에
있습니다. 여기서는 **자주 고치게 되는 것**과 **실수하기 쉬운 것**만
다룹니다.

```bat
REM 설정이 올바른지 검사
scripts\doctor.bat
REM 또는
.venv\Scripts\python -m gymhall config check
```

잘못된 값은 **한꺼번에 모아서** 알려 줍니다. 하나 고치고 다시 돌리는
일을 반복하지 않아도 됩니다.

---

## 장소마다 설정을 다르게 쓰기

집 데스크탑과 강의실 노트북에서 설정이 달라야 한다면, 저장소를 두 번
clone 하지 말고 **환경변수로 설정 파일을 바꿔치기** 하세요.

```bat
set GYMHALL_CONFIG=D:\설정\노트북.toml
scripts\record_now.bat
```

`general.machine_label` 을 서로 다르게 두면 릴리스 에셋 이름에
구분자가 붙어 어느 PC에서 올린 것인지 알 수 있습니다.

> `machine_label` 은 에셋 이름에 들어가므로 **영문/숫자**로 두세요.
> GitHub 가 비ASCII 문자를 버리기 때문에 한글로 두면 사라집니다.

---

## 녹화

### 용량을 줄이고 싶을 때

슬라이드 위주 강의는 아래 정도로 충분히 읽힙니다.

```toml
[recording.video]
fps          = 5
crf          = 32
scale_width  = 1280
preset       = "veryfast"
```

1시간에 대략 150~400MB 수준이 됩니다. `fps` 를 올리거나 `crf` 를
내리면 빠르게 커집니다.

### 세그먼트 길이

```toml
[recording]
segment_minutes = 30
```

| 값 | 결과 |
|---|---|
| 15 | 파일이 많아지지만 크래시 피해가 가장 작음 |
| 30 | 기본값. 2GiB 를 거의 넘지 않음 |
| 60 | 고화질이면 2GiB 를 넘어 분할 업로드 발생 |
| 0 | 끊지 않음. **권하지 않습니다** |

### 오디오 트랙 구성

```toml
[recording.audio]
track_layout = "separate"
```

- `separate` — 시스템/마이크를 별도 트랙. 나중에 한쪽만 쓸 수 있습니다.
- `mixed` — 하나로 합침. 파일이 작지만 분리 불가, 전사 품질도 떨어집니다.
- `both` — 둘 다. 트랙 3개.

`separate` 를 권합니다. mkv 는 트랙이 여러 개여도 대부분의 플레이어가
잘 재생하고, VLC 에서 `오디오 > 오디오 트랙` 으로 고를 수 있습니다.

### 녹화 트리거

네 가지를 동시에 켜 둬도 됩니다. 단, 같은 시각에 둘이 겹치면
**두 번째는 무시**됩니다 (작업 스케줄러의 `IgnoreNew`).

```toml
[recording.triggers]
manual           = true    # 항상 쓸 수 있음
schedule_enabled = false   # 시간표
watch_enabled    = false   # 프로그램 감지
```

시간표는 이렇게 씁니다.

```toml
[[recording.timetable]]
name  = "자료구조"
day   = "mon"        # mon tue wed thu fri sat sun
start = "09:00"
end   = "10:50"

[[recording.timetable]]
name  = "운영체제"
day   = "wed"
start = "13:00"
end   = "14:50"
```

```bat
REM 등록한 시간표와 다음 녹화 시각 확인
scripts\show_timetable.bat
```

---

## 전사

### 저사양 노트북 권장 조합

```toml
[transcribe]
engine       = "faster-whisper"
language     = "ko"
source_track = "system"

[transcribe.batch]
model = "small"
mode  = "queue"      # 쌓아 뒀다가 충전 중에 처리
device = "cpu"
compute_type = "int8"
cpu_threads = 0      # 0 = 코어 수 - 1

[transcribe.realtime]
enabled = false      # 켜지 마세요
```

### 실시간 전사를 꼭 켜야 한다면

```toml
[transcribe.realtime]
enabled     = true
model       = "tiny"
cpu_threads = 2
chunk_sec   = 10     # 길게 잡을수록 문장이 덜 잘립니다

[recording.video]
fps = 5              # 반드시 같이 낮추세요
```

실시간 전사가 입력을 못 따라가면 로그에 경고가 남습니다.
그 경고가 보이면 끄는 게 맞습니다.

---

## 업로드

### 태그 전략

```toml
[github]
tag_strategy        = "monthly"   # 녹화
tag_strategy_eclass = "daily"     # e-class 자료 -> eclass-2026-11-13
```

e-class 자료는 기본으로 **날짜별** 릴리스(`eclass-년-월-일`)에 올라갑니다.
같은 파일은 업로드 기록으로 걸러지므로 날마다 새로 받은 것만 그날 릴리스에 들어갑니다.
아래 값은 두 키 모두에 쓸 수 있습니다.

| 값 | 태그 예시 | 언제 |
|---|---|---|
| `monthly` | `recordings-2026-11` | 기본. 태그가 1년에 12개 |
| `weekly` | `recordings-2026-W46` | 과목이 많고 자료가 자주 올라올 때 |
| `daily` | `recordings-2026-11-11` | 태그가 금방 수백 개가 됩니다 |
| `single` | `recordings-archive` | 전부 한 릴리스에. 에셋이 너무 많아집니다 |

### 민감한 것은 올리지 않기

```toml
[eclass.upload]
exclude_menus    = ["grade", "attendance"]
allow_extensions = ["pdf", "pptx", "hwp", "docx", "md"]
max_file_mb      = 0     # 0 = 제한 없음
```

### 업로드 없이 수집만

```bat
scripts\eclass_sync.bat --no-upload
```

또는 설정에서:

```toml
[eclass.upload]
enabled = false
```

### 업로드 흉내만 내 보기

실제로 올리지 않고 무엇이 올라갈지 확인합니다.

```bat
.venv\Scripts\python -m gymhall eclass sync --dry-run
```

---

## 데이터 폴더 옮기기

```toml
[general]
data_dir = "D:\\강의자료"
```

- 역슬래시는 **두 번** 써야 합니다 (`\\`). 또는 슬래시(`/`)를 쓰세요.
- `%USERPROFILE%` 같은 환경변수도 씁니다.
- **OneDrive/드롭박스 폴더는 피하세요.** 녹화 중인 파일을 동기화하려
  들면서 충돌하고, 수 GB 를 클라우드에 올려 버립니다.
