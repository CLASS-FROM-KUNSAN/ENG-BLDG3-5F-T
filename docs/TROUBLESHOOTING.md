# 안 될 때

**먼저 `scripts\doctor.bat` 을 돌리세요.** 여기 적힌 문제의 대부분을
자동으로 짚어 줍니다.

그다음 **로그 파일**을 보세요. 작업 스케줄러로 돌리면 창이 안 뜨므로
로그가 유일한 단서입니다. 위치는 `doctor.bat` 이 알려 주는 데이터 폴더의
`logs\syncnote.log` 입니다.

---

## 녹화

### 마이크가 '녹음 중 오류 AssertionError' 로 안 열립니다(메인보드 마이크 단자)

메인보드 마이크 단자(`마이크(High Definition Audio Device)` 등) 드라이버 중에는 Windows 공유
모드 형식을 `IEEE_FLOAT`(포장 없음)으로 알려 주는 것이 있습니다. 녹음 라이브러리 soundcard 는
`WAVE_FORMAT_EXTENSIBLE` 만 받아들여 이 장치를 열지 못합니다(권한 문제가 아닙니다).

- **ffmpeg 가 있으면 저절로 같은 장치를 ffmpeg DirectShow 로 엽니다.** 로그에
  `→ 같은 장치를 ffmpeg(DirectShow)로 엽니다` 가 찍힙니다.
- ffmpeg 도 안 되면 SoX(Windows `waveaudio`), 리눅스는 arecord 까지 차례로 시도합니다.
  녹음 중 끊겨도 다음 방법으로 이어 받고 끊긴 시간은 무음으로 채웁니다(화면과 안 어긋남).
  녹음이 끝나면 로그에 `녹음 중 방법을 바꿨습니다 … soundcard 끊김 → ffmpeg DirectShow 로 엶`
  처럼 무엇이 있었는지 남습니다.
- 마이크 권한이 꺼져 있으면 녹화가 알리고 켭니다(이 사용자 항목). 이 PC 전체 항목은
  관리자 권한이 필요합니다. 직접: `python -m syncnote devices --allow-mic`
- 처음부터 ffmpeg 로 열게 고정하려면 `[recording.audio] backend = "dshow"`.
- 마이크 권한은 짐작하지 않고 Windows 설정을 실제로 읽어, 꺼져 있을 때만 권한을 안내합니다.
- 최대레벨이 -60 dB(0.001) 아래면 "아주 작음" 경고가 나옵니다 — 단자에 아무것도 안
  꽂혔거나 입력 볼륨이 0 에 가까운 경우입니다.

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

### 녹음 장치가 `스테레오 믹스` 뿐일 때 (메인보드 마이크 단자)

메인보드 마이크 단자(3.5mm, 분홍색)는 소리 파형만 주고받아 꽂은 기기의 이름을 PC 가 알 수
없습니다. 그래서 Realtek 드라이버가 단자를 '마이크 입력' 으로 바꾸지 못하면 녹음 장치가
`스테레오 믹스(Realtek(R) Audio)` 만 보입니다. 강의실 PC 에서 실제로 확인했습니다.
- 그 PC 는 Windows 기본 드라이버만 잡혀 있었습니다.
- 그래서 'Realtek Audio Console 이 이 기계에서 지원되지 않습니다' 가 떴습니다.
- 메인보드 제조사의 Realtek 드라이버를 설치해야 했습니다.

**녹음**
- 스테레오 믹스는 기본 마이크로 고르지 않습니다. 진짜 마이크가 있으면 그것이 마이크입니다.
- 스테레오 믹스는 **따로 한 트랙으로 함께 녹음**해 마이크 트랙에 겹칩니다. 둘 다 받은 소리가
  함께 들립니다.
- 진짜 마이크가 없으면 스테레오 믹스가 마이크 자리를 대신합니다.
- 끄려면 `recording.audio.stereo_mix_enabled = false` 로 둡니다.

**송출**
- 마이크 입력이 없으면 스테레오 믹스로 송출합니다. 송출은 입력이 하나라 둘을 겹치지는 않습니다.

**안내 (사람이 직접 해야 함)**

기본 녹음 장치가 스테레오 믹스이고 마이크 입력이 없으면 안내가 나옵니다. 다른 녹음 장치가
없거나, 있어도 시험에서 전부 신호가 없을 때입니다.

안내가 나오는 곳:
- `devices.bat`
- `doctor` (`[X] 마이크 입력 없음`, 시간표 편집기의 점검에도 나옴)
- `setup.bat`
- 녹화 로그, 송출 로그

안내 내용:
1. 드라이버를 설치하거나 업데이트합니다. 다음 중 하나를 하세요.
   - https://www.realtek.com/Download/List?cate_id=593 에서 드라이버 설치 파일을 받아 실행합니다.
   - 장치 관리자 → '사운드, 비디오 및 게임 컨트롤러' → Realtek(R) Audio → [드라이버 업데이트]
2. **이 PC 의 메인보드**를 함께 보여 줍니다. msinfo32 의 '베이스보드 제조업체·제품' 과 같은
   값입니다. 메인보드 제조사의 오디오 드라이버가 가장 정확합니다.
   - 사람이 실행한 `devices.bat`·`doctor.bat`·`setup.bat` 은 그 보드의 드라이버 검색(Google)을
     **Chromium 으로 띄웁니다**. Chromium 이 없으면 기본 브라우저로 띄웁니다.
   - 녹화·송출·작업 스케줄러에서는 브라우저를 띄우지 않고 링크만 남깁니다. 수업 중 화면에 창이
     뜨지 않게 하려는 것입니다.
3. 그래도 마이크가 안 보이면 다음을 확인합니다.
   - mmsys.cpl → [녹음] 탭 → '사용할 수 없는 장치 표시'·'연결이 끊긴 장치 표시' 를 켭니다.
     '마이크(Realtek(R) Audio)' 가 보이면 [사용]·[기본 장치로 설정] 을 누릅니다.
   - Realtek Audio Console → 장치 고급 설정에서 단자가 '마이크 입력(Mic In)' 인지 봅니다.
   - 플러그의 검은 띠가 3개(4극, 스마트폰용)면 4극→3극 Y 분리 젠더로 분홍색 단자에 꽂습니다.

드라이버를 대신 받거나 실행하지는 않습니다.

- 원격(Jump Desktop)으로 접속해 있으면 내 PC 의 마이크는 원격 PC 로 넘어가지 않습니다.
  강의실 마이크를 녹음하려면 강의실 PC 에 마이크를 꽂아야 합니다.

### Playwright 브라우저 받기 (`self-signed certificate in certificate chain`)

`playwright install chromium` 이 다음처럼 끝나면 학교·회사망 장비가 HTTPS 를 중간에서
풀어 보고 자기 인증서로 다시 감싸는 경우입니다.

```
Error: self-signed certificate in certificate chain   { code: 'SELF_SIGNED_CERT_IN_CHAIN' }
```

그 인증서는 보통 Windows 인증서 저장소에 들어 있어 브라우저는 괜찮은데, Playwright 의
다운로더(Node.js)는 Windows 저장소를 보지 않아 거부합니다.

- `scripts\setup.bat`(과 설치·점검 창)은 `python -m syncnote.playwright_setup` 으로 받습니다.
  이 오류가 나면 **Windows 인증서 저장소(ROOT·CA)를 PEM 으로 꺼내 `NODE_EXTRA_CA_CERTS` 로
  넘겨 한 번 더** 받습니다. 검증을 끄지는 않습니다(`NODE_TLS_REJECT_UNAUTHORIZED=0` 은 쓰지 않음).
- 직접 하려면: `.venv\Scripts\python.exe -m syncnote.playwright_setup`
- 그래도 안 되면 그 망의 검사용 루트 인증서가 Windows 에 없는 것입니다. 전산실에서 받아
  '신뢰할 수 있는 루트 인증 기관' 에 설치하거나, 다른 망(테더링 등)에서 다시 실행하세요.
- 일반 Chromium 만 받아지고 `chrome-headless-shell` 만 실패했어도 수집은 됩니다.
  headless shell 이 없으면 일반 Chromium 을 창 없이 띄웁니다(로그에 경고 한 줄).

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

### 녹화 도중 컴퓨터가 꺼졌는데 릴리스에 안 올라갔습니다

녹화 중에는 화면과 소리를 세션 폴더의 `.work` 에 따로 쓰고, 세그먼트가 정상으로
끝날 때 합쳐서 올립니다. 꺼지면 합치기·올리기가 일어나지 않습니다.

- **다시 켜고 로그온하면 저절로 처리됩니다.** `RecordSchedule`·`RecordWatch` 가
  시작하면서 `.work` 에 남은 조각을 찾아 WAV 머리를 고치고, 파일(mkv/mp4)로 합쳐,
  **녹화한 날짜의 릴리스**(`recordings-YYYY-MM-DD`)에 올립니다. 화면 파일이 망가졌으면
  소리만으로라도 만듭니다. 쓸 수 없던 조각은 `.work\leftover` 로 옮겨 둡니다.
- 올리다 실패했거나(네트워크) 올리는 중에 꺼진 파일도 `state\recording_outbox.json`
  목록에 남아 있다가 다음 시작 때 다시 올립니다.
- 바로 확인·처리하려면:

  ```bat
  python -m syncnote record recover --list    REM 무엇이 남았는지만
  python -m syncnote record recover           REM 지금 파일로 만들고 올리기
  ```

- 로그에 `끝나지 않은 녹화 조각을 찾았습니다` 가 찍히면 복구가 돈 것입니다.
- **녹화 지킴이**(`RecordGuard` 작업, `[recording.guard]`)가 녹화 중이 아닐 때 5분마다
  모든 녹화 파일을 `파일 → 검사 통과 → 올라감 → 확인됨` 으로 모읍니다.
  - 검사는 파일을 처음부터 끝까지 풀어 봅니다. 담는 틀이 망가졌으면 화질 손실 없이
    다시 담고(remux), 아예 열리지 않으면 `audio\` 의 WAV 로 소리만이라도 만듭니다.
    원본은 `.work\leftover` 에 남깁니다. 화면 몇 장만 깨진 파일은 그대로 올립니다.
  - 올린 뒤 릴리스에 같은 이름·크기의 파일이 정말 있는지 확인하고, 없거나 다르면 다시 올립니다.
  - 지금 상태: `python -m syncnote record guard --status` (doctor 에도 한 줄 나옴)
  - `격리` 는 여러 번 실패한 것(하루에 한 번 다시 시도), `막힘` 은 올릴 곳이 없는 것
    (과목을 모름 · public 저장소 차단 — `recording.upload.block_public`, 기본 꺼짐)입니다.
- 마지막 몇 초(ffmpeg 가 아직 디스크에 쓰지 못한 부분)는 되살릴 수 없습니다.

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
