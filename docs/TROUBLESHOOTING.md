# 안 될 때

**먼저 `scripts\doctor.bat` 을 돌리세요.** 여기 적힌 문제의 대부분을
자동으로 짚어 줍니다.

그다음 **로그 파일**을 보세요. 작업 스케줄러로 돌리면 창이 안 뜨므로
로그가 유일한 단서입니다. 위치는 `doctor.bat` 이 알려 주는 데이터 폴더의
`logs\syncnote.log` 입니다.

---

## 녹화

### 소리가 하나도 안 들어갑니다

가장 흔한 사고입니다. 순서대로 확인하세요.

1. `scripts\devices.bat` 을 돌려 **루프백** 항목이 있는지 봅니다.
2. `config.toml` 의 `recording.audio.system_device` 에 적은 이름이
   거기 나온 이름과 일치하는지 봅니다. **부분 일치**로 찾으므로 전체를
   적을 필요는 없습니다.
3. 비워 두면 **현재 기본 재생 장치**를 씁니다. 이어폰을 꽂았다 뺐다 하면
   기본 장치가 바뀌므로, 이름을 명시하는 편이 안전합니다.
4. 로그에 `전 구간 무음` 경고가 있는지 확인합니다. 녹음 시작 60초 뒤에
   한 번 남깁니다.

```bat
REM 1분 시험 녹화로 확인
scripts\record_now.bat -m 1 --no-upload
```

### 마이크 소리만 들어가고 강의 소리가 없습니다

`system_device` 가 아니라 마이크를 잡은 것입니다. `devices.bat` 에서
**[loopback]** 으로 표시된 이름을 쓰세요. `[microphone]` 이 아닙니다.

### 루프백 장치가 아예 안 보입니다

- `soundcard` 가 설치돼 있는지: `pip install soundcard`
- 스피커가 하나도 없는 경우(헤드리스 PC 등)에는 루프백도 없습니다.
- 그래도 안 되면 `recording.audio.backend = "dshow"` 로 바꾸고
  [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) 을 설치하는
  경로가 있습니다. 설치가 필요해지므로 권하지는 않습니다.

### 마이크가 없는 PC (`스테레오 믹스` 만 있음)

`Get-PnpDevice -Class AudioEndpoint` 에 마이크는 없고 `스테레오 믹스(Realtek(R) Audio)` 만
보이는 PC 가 있습니다. 스테레오 믹스는 입력 장치처럼 보이지만 **스피커로 나가는 소리를
다시 녹음**합니다. 시스템 소리는 이미 루프백으로 따로 녹음하므로, 이것을 마이크로 쓰면 같은
소리가 두 트랙에 들어갑니다.

- 자동 선택은 `스테레오 믹스`·`Stereo Mix`·`What U Hear` 류를 마이크로 고르지 않습니다.
  진짜 마이크가 없으면 마이크 트랙 없이 **시스템 소리와 화면만** 녹화하고 경고를 한 번 남깁니다.
- `python -m syncnote devices` 에 그 장치는 "마이크로 자동 선택하지 않음" 으로 표시됩니다.
- 그래도 쓰고 싶으면 `recording.audio.mic_device` 에 이름을 직접 적으세요(사람이 고른 것은 그대로).
- 원격(Jump Desktop)으로 접속해 있으면 내 PC 의 마이크는 원격 PC 로 넘어가지 않습니다.
  강의실 마이크를 녹음하려면 강의실 PC 에 마이크를 꽂아야 합니다.
### ffmpeg 를 찾지 못합니다

```bat
scripts\install_ffmpeg.bat
```

winget(`Gyan.FFmpeg`)으로 설치하고, 안 되면 릴리스 목록에서 새 버전부터 받아
해시·기능 확인에 실패하면 이전 릴리스로 롤백합니다. 끝에 '시도한 순서와 실패 이유'
가 나오니 그것을 보세요. 직접 하려면
PowerShell 에서 `winget install Gyan.FFmpeg`, 또는 받아서 환경변수 `FFMPEG` 에
`ffmpeg.exe` 전체 경로를 넣으세요.

`winget` 으로 깔았는데 `ffmpeg -version` 이 '인식되지 않습니다' 라면 그 창이 설치
전에 열린 것입니다. 창을 새로 여세요. syncnote 자체는 창을 다시 열지 않아도 winget 이
둔 위치를 찾습니다.

### ffmpeg 가 없는데 녹화 시간이 됐습니다

수업을 통째로 잃지 않게 **소리만 녹음합니다**(soundcard → WAV). 화면은 ffmpeg 가
있어야 잡을 수 있어 녹화되지 않습니다. 로그와 녹화 결과에
"ffmpeg 가 없어 소리만 녹음합니다" 가 한 번 남습니다.

- 세그먼트마다 WAV 가 `audio\` 에 남고, 옆에 `<세그먼트>.pending.json` 이 생깁니다.
  전사 설정(`transcribe.enabled`)이 꺼져 있어도 이 WAV 는 지우지 않습니다.
- ffmpeg 를 설치한 뒤(`scripts\install_ffmpeg.bat`) 파일로 만드세요.

```bat
python -m syncnote record remux --dry-run   REM 만들 목록만
python -m syncnote record remux             REM 소리 트랙 mkv/mp4 + 수업 태그·챕터
python -m syncnote record remux --upload    REM 만든 파일을 녹화 때처럼 올림
```

만들다 실패한 것은 `pending.json` 이 남아 다음에 다시 시도합니다. 올릴 때는 녹화 때와
같은 규칙입니다(과목을 모르면 `course_repos` 가 있을 때 올리지 않음).
### 영상이 검은 화면입니다

작업 스케줄러에서 **"사용자의 로그온 여부에 관계없이 실행"** 으로 설정한
경우입니다. 화면 캡처는 로그온된 세션이 필요합니다.
`register_tasks.ps1` 이 등록한 작업은 `Interactive` 로 돼 있으니,
손으로 바꾸지 마세요.

### 프레임이 뚝뚝 끊깁니다

저사양 PC에서 실시간 전사를 켠 경우가 대부분입니다.

```toml
[transcribe.realtime]
enabled = false      # 이걸 끄세요

[recording.video]
fps = 5              # 그래도 부족하면
crf = 32
scale_width = 1280
preset = "ultrafast"
```

### 영상과 소리가 어긋납니다

세그먼트 시작 시각 차이를 `-itsoffset` 으로 보정하고 있습니다. 그래도
어긋난다면 `--log-level DEBUG` 로 돌려 로그의 offset 값을 확인하고
이슈로 남겨 주세요.

### 녹화가 중간에 멈춥니다

- `max_duration_minutes` 에 도달했는지 확인합니다 (기본 240분).
- 디스크가 찼는지 확인합니다. 1시간에 수백 MB ~ 수 GB 가 쌓입니다.
- 절전 모드로 들어갔다면 그 시간 동안은 녹화되지 않습니다.
  전원 옵션에서 "화면 끄기"는 괜찮지만 "절전 모드"는 안 됩니다.

---

## Eclass

### 로그인에 실패합니다

- `eclass.username` 이 학번인지 확인하세요.
- `scripts\set_password.bat` 으로 비밀번호를 다시 저장하세요.
- **자동 재시도를 하지 않습니다.** 계정 잠금을 피하기 위해서입니다.
  여러 번 실패했다면 브라우저로 직접 로그인해 계정 상태를 확인하세요.

```bat
REM 브라우저 창을 띄워 직접 보기
scripts\eclass_probe.bat
```

### 과목 목록이 비어 있습니다

로그인은 됐는데 과목을 못 찾는 경우입니다. 학교가 메인 페이지를
바꿨을 가능성이 큽니다.

1. `scripts\eclass_probe.bat` 실행
2. 결과 폴더의 `02-home-after-login.html` 에서
   `select[name="courseDTO.courseId"]` 가 아직 있는지 확인
3. 없다면 `src/syncnote/eclass/crawler.py` 의 `list_courses()` 선택자 수정

### 특정 메뉴가 수집되지 않습니다

과목에 그 메뉴가 개설돼 있지 않으면 그냥 건너뜁니다(로그에 남습니다).
정상입니다.

모든 과목에서 같은 메뉴가 안 되면 `menuId` 가 바뀐 것입니다.
`eclass_probe.bat` 결과의 `report.json` 에서 `menus_found` 를 보고
`config.toml` 의 `[eclass.menu_ids]` 에 새 값을 넣으세요.

### 첨부파일이 안 받아집니다

이 LMS 는 숨은 iframe 으로 파일을 내려보냅니다. 브라우저 창을 띄워
직접 보세요.

```bat
.venv\Scripts\python -m syncnote --log-level DEBUG eclass sync --no-headless --no-upload
```

### 같은 파일을 계속 다시 받습니다

교수가 파일을 같은 이름으로 교체하면 내용 해시가 달라져 새 파일로
인식합니다. 의도된 동작입니다.

그게 아니라면 상태 파일이 저장되지 않는 것입니다. 데이터 폴더의
`state\eclass.json` 이 갱신되는지 확인하세요.

---

## 업로드

### 401 Unauthorized

GitHub 토큰이 만료됐거나 권한이 없습니다. `repo` 범위가 필요합니다.

```bat
scripts\set_password.bat
REM [2] GitHub 토큰
```

### 파일이 너무 큽니다 / 업로드가 거부됩니다

GitHub 릴리스 에셋 상한은 **2GiB** 입니다. 이 도구는 자동으로
`.partNNN` 으로 쪼개 올립니다. 그래도 거부된다면
`github.split_size_mb` 를 1500 정도로 낮춰 보세요.

복원은 릴리스에 함께 올라간 `*.parts.json` 을 보고:

```bat
copy /b "이름.part*" "원본이름.mkv"
```

### 한글 파일명이 깨져서 올라갑니다

GitHub 가 에셋 이름의 비ASCII 문자를 버립니다. 그래서 안전한 이름 +
원본 이름 해시 8자리로 바꿔 올립니다. **원래 이름은 에셋의 label 에
그대로 남아 있습니다.** 릴리스 페이지에서 파일 위에 마우스를 올리면
보입니다.

### 업로드가 자꾸 끊깁니다

```toml
[github]
retry_count   = 6
retry_backoff = 3.0
split_size_mb = 800   # 작게 쪼개면 한 번에 올리는 양이 줄어듭니다
```

---

## 전사

### 모델 내려받기에서 멈춥니다

처음 쓸 때 수백 MB ~ 수 GB 를 받습니다. 인터넷이 되는 곳에서 미리:

```bat
.venv\Scripts\python -m syncnote transcribe warmup
```

### 너무 느립니다

```toml
[transcribe.batch]
model       = "base"   # small -> base
cpu_threads = 0        # 0 = 자동 (코어 수 - 1)
vad_filter  = true     # 무음 구간을 건너뜁니다
```

저사양 노트북 기준 1시간 음성 처리 시간 대략치:

| 모델 | 소요 |
|---|---|
| tiny | 약 2분 |
| base | 약 4분 |
| small | 약 10분 |
| medium | 30분 이상 |

### 전사 결과가 엉망입니다

- 원본 소리가 작거나 잡음이 많으면 어쩔 수 없습니다.
  `system_gain_db` 로 볼륨을 올려 보세요.
- `source_track` 이 맞는지 확인하세요. 온라인 강의는 `system`,
  교실 녹음은 `mic` 입니다.
- 실시간 전사 결과를 보고 있는 것일 수 있습니다. 파일 이름이
  `_realtime.txt` 로 끝나면 임시본입니다.
- `language = "ko"` 로 고정하는 편이 `auto` 보다 정확합니다.

### CUDA 오류가 납니다

`device = "cpu"` 로 바꾸세요. GPU 를 쓰려면 CUDA 12 와 cuDNN 9 를
따로 설치해야 하고, pip 로는 해결되지 않습니다.

---

## 작업 스케줄러

### 작업이 실행되지 않습니다

1. 작업 스케줄러에서 `Sync Note` 폴더의 작업을 마우스 오른쪽 클릭
   → **실행** 으로 직접 돌려 봅니다.
2. "마지막 실행 결과" 코드를 봅니다.
3. 그래도 모르겠으면 `.vbs` 를 거치지 않고 `.bat` 을 직접 실행해
   창에 뜨는 오류를 봅니다.

### 검은 창이 깜빡입니다

`.bat` 을 직접 등록한 경우입니다. `register_tasks.ps1` 이 등록한 것처럼
`wscript.exe hidden.vbs "...bat"` 형태로 바꾸세요.

### 노트북을 켜면 밀린 작업이 한꺼번에 돕니다

`StartWhenAvailable` 때문입니다. Eclass 수집은 이게 맞지만, 시간표
녹화가 뒤늦게 시작되는 건 곤란합니다. 녹화는 수동 실행을 기본으로
쓰는 편이 안전합니다.

---

## 그래도 안 되면

```bat
.venv\Scripts\python -m syncnote --log-level DEBUG <명령>
```

으로 돌린 로그를 이슈에 붙여 주세요. **로그에서 학번, 이름, 토큰이
들어간 줄은 지우고** 올리세요.
