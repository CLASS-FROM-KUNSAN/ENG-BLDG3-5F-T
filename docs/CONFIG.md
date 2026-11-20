# 설정 안내

전체 항목과 주석은 [`config.example.toml`](../config.example.toml) 에
있습니다. 여기서는 **자주 고치게 되는 것**과 **실수하기 쉬운 것**만
다룹니다.

```bat
REM 설정이 올바른지 검사
scripts\doctor.bat
REM 또는
.venv\Scripts\python -m syncnote config check
```

잘못된 값은 **한꺼번에 모아서** 알려 줍니다. 하나 고치고 다시 돌리는
일을 반복하지 않아도 됩니다.

---

## 장소마다 설정을 다르게 쓰기

집 데스크탑과 강의실 노트북에서 설정이 달라야 한다면, 저장소를 두 번
clone 하지 말고 **환경변수로 설정 파일을 바꿔치기** 하세요.

```bat
set SYNCNOTE_CONFIG=D:\설정\노트북.toml
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

### 시간표 편집기 (GUI)

손으로 적는 대신 화면에서 편집할 수 있습니다. PySide6 가 필요합니다
(`pip install PySide6`).

```bat
scripts\timetable_edit.bat
REM 또는: python -m syncnote timetable edit [-c config.toml]
```

- **시간표 탭**: 빈 칸을 드래그하면 수업 추가, 블록을 끌면 이동, 아래
  가장자리를 끌면 길이 조절, 더블클릭하면 수정. 5분 단위로 맞춥니다.
  여러 요일 수업은 시간이 함께 움직이고, **Shift** 를 누른 채 끌면 그
  요일만 떼어 옮깁니다. 시간이 겹치는 수업은 나란히 그리고 빨간 테두리로 표시합니다
  (녹화는 한 번에 하나라 뒤 수업이 빠질 수 있습니다).
- **수업 목록 탭**: 표로 보고 추가/수정/복제/삭제.
- **공휴일 탭**: 쉬는 날. 그날 수업은 녹화·송출 일정에서 건너뜁니다.
  "3월 1일(일) 삼일절" 같은 목록을 붙여넣어 한 번에 넣을 수 있습니다.
- **파일 메뉴**: 저장(Ctrl+S), iPad 시간표 앱(Swift) 백업 JSON 가져오기/내보내기.
- Ctrl+Z / Ctrl+Y 로 실행 취소 / 다시 실행.
- **eClass 과목 연결**: 수업 창의 `eClass` 줄이 수업명과 가장 비슷한 eClass 과목을
  제안합니다(띄어쓰기·음절 순서 무관). `적용` 을 누르면 eClass 정식 과목명과
  과목코드로 채웁니다. 편집 메뉴 `eClass 과목과 연결…` 은 과목코드가 빈 수업을
  한꺼번에 연결합니다(확신 있는 것만 미리 체크, 한 번에 실행 취소).
  - 과목 목록은 eClass 수집(`scripts\eclass_sync.bat`)을 한 번 돌리면
    `<data_dir>/state/eclass_courses.json` 에 남습니다(과목코드·과목명·학기만,
    교수 이름은 남기지 않음). 그 전에는 `[[eclass.course_repos]]` 와 수집 폴더
    이름만 씁니다.
  - 과목코드가 있으면 녹화 파일이 어느 과목인지 이름이 조금 달라도 정확히 판정됩니다.

**파일 → 설정 편집…(Ctrl+,)** 으로 언제든 설정을 고칠 수 있습니다. config.toml 이
있으면 지금 값을 보여 주고, 저장하면 바꾼 줄만 고칩니다(주석·시간표 그대로, 원본은
`.bak`). config.toml 에 없는 항목을 바꾸면 그 섹션 끝에 넣고, 섹션이 없으면 새로
만듭니다. 창을 연 사이 파일이 다른 곳에서 바뀌었으면 저장하지 않습니다.
`예시 값으로` 는 config.example.toml 의 값으로 되돌립니다.

`config.example.toml` 은 편집기를 열 때 읽어 둡니다. 예시 파일을 고쳤다면
**파일 → 디스크에서 다시 읽기(F5)** 로 config.toml 과 함께 다시 읽으세요.

**config.toml 이 아직 없으면** 저장 전에 `새 config.toml 만들기` 창이 뜹니다.

- 설정 목록·기본값·설명·분류를 전부 `config.example.toml` 에서 읽습니다
  (코드에 목록이 없습니다 — 예시 파일을 고치면 창도 따라 바뀝니다).
- 왼쪽: 분류(공통 → eClass 수집 → 릴리스 업로드 → 녹화 → 전사 → 송출 → 백업 →
  활동 기록) 와 섹션. 맨 위 **먼저 확인할 항목** 은 실제 설정 검사에 걸린
  항목만 모읍니다(예: 학번이 비어 있음).
- 오른쪽: 항목마다 설명 첫 줄을 제목으로, 나머지 설명과 키 이름을 같이
  보여 줍니다. 켜기/끄기는 체크, 숫자는 숫자 칸, 목록은 한 줄에 하나.
  바꾼 항목은 파란 줄, 검사에 걸린 항목은 빨간 줄. `기본값으로` 로 되돌림.
- 위쪽 검색과 `바꾼 항목만`, 아래쪽 실시간 설정 검사(누르면 그 항목으로 이동).
- `만들기` 를 누르면 예시 파일 **본문 그대로**에 바꾼 값만 그 줄에 넣어
  config.toml 을 만들고(설명 주석이 그대로 남음), 그 뒤에 시간표를 붙입니다.
  취소하면 아무 파일도 만들지 않습니다.

저장하면 config.toml **맨 아래 표시줄 사이만** 다시 씁니다.

```toml
# >>> syncnote 시간표 편집기 (이 아래부터 END 까지는 편집기가 다시 씁니다) >>>
[[recording.timetable]]
name  = "자료구조"
day   = "mon"
start = "09:00"
end   = "10:50"
room  = "공학관 401호"

[[recording.holidays]]
date = "2026-12-25"
name = "성탄절"
# <<< syncnote 시간표 편집기 <<<
```

- 표시줄 밖의 설정과 주석은 그대로 둡니다. 원본은 `config.toml.bak` 으로 남습니다.
- 손으로 적어 둔 `[[recording.timetable]]` 이 있으면 같은 수업이 두 번
  잡히지 않도록 **주석 처리**합니다(지우지 않음).
- `room`·`professor`·`color`·`course_id` 는 편집기 표시용 칸입니다.
  녹화에는 `name/day/start/end/language` 만 쓰입니다(`course_id` 는 과목 판정에도 쓰임).
- 수업 추가/수정 창은 `src/syncnote/timetable/course_dialog.ui` 입니다.
  `pyside6-designer` 로 열어 드래그 앤 드롭으로 배치를 바꿀 수 있습니다
  (위젯 이름만 그대로 두면 됩니다).

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
.venv\Scripts\python -m syncnote eclass sync --dry-run
```

---

## 강의실 PC 홈 백업

```toml
[backup.home]
enabled = true          # 기본 false

[backup.home.prune]
enabled = true          # 기본 false — 켜면 올라간 파일을 로컬에서 지웁니다
min_age_hours = 24
```

- `python -m syncnote backup home` 이 `%USERPROFILE%` 를 `home-2026-11-13` 릴리스에 올립니다.
  홈 바로 아래 `AppData` 와 `.` 폴더, 정션, OneDrive 클라우드 전용 파일, `exclude_names` 는 빠집니다.
- 올릴 때마다 매니페스트(어느 디렉터리의 어느 파일이 어느 묶음에 있는지)를 같은 릴리스에 올리고,
  앞 매니페스트의 sha256 으로 사슬을 잇습니다. 매니페스트 하나에는 최대 5만 개까지 들어갑니다.
- `python -m syncnote backup prune` 은 GitHub 의 사슬을 내려받아 확인한 뒤, 지금 파일의 해시가
  사슬의 값과 같은 것만 지웁니다. 디렉터리는 남깁니다. 먼저 `--dry-run` 으로 목록을 보세요.
- 상태는 `state/home_backup.sqlite` 에 있습니다(파일 수십만 개라 JSON 이 아닙니다).
- 업로드/삭제 목록: `logs/home-chain.log`, `logs/home-prune.log`.
  자세한 것은 [배포 문서](DEPLOYMENT.md) 를 보세요.

## 데이터 폴더 옮기기

```toml
[general]
data_dir = "D:\\강의자료"
```

- 역슬래시는 **두 번** 써야 합니다 (`\\`). 또는 슬래시(`/`)를 쓰세요.
- `%USERPROFILE%` 같은 환경변수도 씁니다.
- **OneDrive/드롭박스 폴더는 피하세요.** 녹화 중인 파일을 동기화하려
  들면서 충돌하고, 수 GB 를 클라우드에 올려 버립니다.
