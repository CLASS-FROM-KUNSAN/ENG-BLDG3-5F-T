# 설치 상세

`scripts\setup.bat` 이 아래를 전부 자동으로 합니다. 이 문서는 자동
설치가 실패했을 때 손으로 하는 방법입니다.

---

## 1. Python

3.10 이상이 필요합니다. 3.11 이상을 권합니다.

<https://www.python.org/downloads/>

설치할 때 **"Add python.exe to PATH"** 를 반드시 체크하세요.
빠뜨리면 모든 `.bat` 이 실패합니다.

```bat
python --version
```

> **아무것도 안 나오면 `py --version` 을 써 보세요.**
> Windows 에서 `python` 이 `C:\Windows\system32\python` 으로 잡히는
> PC 가 있습니다. Microsoft Store 스텁이라 진짜 인터프리터가 아니고,
> 오류도 없이 조용히 끝납니다. 2026-11-13 실기 PC 가 그랬습니다.
> 그 경우 아래의 `python` 을 전부 `py` 로 읽으세요. venv 를 만든
> 뒤로는 `.venv\Scripts\python` 을 쓰므로 이 문제가 없습니다.


---

## 2. 가상환경과 패키지

```bat
cd <레포 폴더>
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

가상환경을 쓰는 이유: 이 도구가 쓰는 패키지와 다른 프로젝트의 패키지가
충돌하지 않게 합니다. `.bat` 들이 `.venv` 를 자동으로 찾습니다.

---

## 3. Playwright 브라우저

Eclass 수집을 쓸 때만 필요합니다. 약 150MB.

```bat
.venv\Scripts\python -m playwright install chromium
```

> 이 LMS 는 게시글 열람과 첨부 다운로드를 JavaScript 가 숨은 폼을
> POST 하는 방식으로 처리합니다. `requests` 로는 폼 필드를 역설계해야
> 하고 학교가 필드명을 바꾸면 조용히 깨집니다. 실제 브라우저로
> 클릭하는 편이 훨씬 오래 갑니다.

---

## 4. ffmpeg

녹화를 쓸 때만 필요합니다.

```bat
scripts\install_ffmpeg.bat
```

`setup.bat` 도 ffmpeg 가 없으면 이 스크립트를 부릅니다. 실제 일은
`python -m syncnote.ffmpeg_fetch --auto` 가 실패 원인을 보며 차례로 합니다.

0. 이미 쓸 수 있는 ffmpeg 가 있으면 끝 — PATH, `FFMPEG`, `tools\ffmpeg`, winget 폴더, pip 순.
   녹화에 필요한 기능(gdigrab·libx264·aac)이 있는지도 봅니다.
1. **pip 의 `imageio-ffmpeg`** (requirements.txt 에 있음). wheel 안에 ffmpeg 가 들어 있어
   관리자 권한·winget 없이 깔립니다. Windows 빌드는 gyan.dev essentials 9.0.2 로
   필요한 기능을 확인했습니다. `ffprobe` 는 없지만 녹화에는 필요 없습니다.
   (그 전에 **`tools` 폴더에 가져다 둔 `ffmpeg*.zip`** 이 있으면 그것부터 씁니다 — 오프라인·USB.)
2. **winget** 으로 설치: `winget install Gyan.FFmpeg` 와 같습니다. 동의 질문
   (`Do you agree ...? [Y]`)은 옵션으로 전부 자동 승인하므로 멈추지 않습니다.
   winget 이 **없거나 정책으로 막혀 있으면** 다시 해도 같으므로 `tools\.ffmpeg_install.json`
   에 적어 두고 다음부터 건너뜁니다(`--retry-skipped` 로 다시 시도).
3. winget 이 없거나 실패하면 **실제 릴리스 목록에서 받아 `tools\ffmpeg` 에 둡니다**
   (`python -m syncnote.ffmpeg_fetch`). winget 이 받아 오는 것과 같은 저장소
   [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg/releases) 입니다.
   - 순서: 정식 버전 새 것부터(9.1 → 9.0.2 → …) → 날짜별 git 빌드 → gyan.dev
   - 후보마다 SHA-256(릴리스에 적힌 값)과 녹화에 필요한 기능(gdigrab·libx264·aac)을
     확인하고, 하나라도 실패하면 **한 단계 이전 릴리스로 롤백**합니다.
   - 끊기면 2·4·8초 쉬고 다시 받습니다. 디스크가 꽉 찼으면 바로 멈춥니다.
   - 이미 있던 `tools\ffmpeg` 는 새 것이 다 확인된 뒤에만 바꾸고, 바꾸다 실패하면
     되돌려 놓습니다.
   - 목록은 **세 출처를 함께** 읽어 합칩니다. 하나가 막혀도 나머지로 갑니다.

     | 출처 | 주는 것 | 없거나 막히면 |
     |---|---|---|
     | GitHub API | 목록·파일·SHA-256 | 로그인 없이 시간당 60회 제한 |
     | `gh` (로그인돼 있을 때) | 목록 + `view` 로 SHA-256, 받기 대체 | 건너뜀 (로그인을 시키지 않음) |
     | `git ls-remote --tags` | 태그 이름만 (주소는 태그로 만듦) | 건너뜀 |

     - API 와 gh 의 SHA-256 이 다르면 그 버전은 버립니다.
     - git 으로만 찾은 버전은 해시가 없어 **기본으로 쓰지 않습니다.** 예외로 가장 새 정식 버전은
       gyan.dev 가 공개한 해시(같은 빌드)를 빌려 검증합니다. API·gh 가 다 막혀도 됩니다.
     - 해시 없는 버전까지 쓰려면 `--allow-unverified`. 받기 전에 파일이 실제로 있는지 먼저 봅니다.
     - urllib 로 못 받으면 같은 버전을 `gh release download` 로 다시 받습니다(해시 확인 그대로).
     - **두 번 읽기**: 먼저 세 출처를 넓게 읽고(API 는 1쪽 100개), 실제로 시도할 앞쪽 후보
       (`--max`, 기본 6)만 다시 확인합니다. gh 가 있으면 `gh release view`, 해시가 아예 없으면
       API 로 그 태그 하나만 조회합니다. API 남은 한도가 10 미만이면 더 부르지 않습니다.
     - **신뢰 등급**: T3 두 출처 일치 · T2 한 출처 · T1 해시 없음(`--allow-unverified` 때만) ·
       T0 출처끼리 다름(안 씀). 순서는 등급이 아니라 정식 버전 → 개발 빌드, 새 것부터입니다.
       2025년 이전에 올라간 파일(7.x 이하)은 GitHub 에 해시가 없어 T1 로 남습니다.
     - **받기 전·받는 중**
       - 남은 디스크가 파일 크기의 2.5배(zip + 압축 푼 것 + 바꾸는 동안의 옛것)보다 적으면
         받기 전에 멈춥니다.
       - 끊기면 받은 데까지 남겨 두고 이어받습니다(HTTP Range). 해시가 있는 버전은
         `tools\_downloads` 에 남겨 **다음 실행에서도** 이어받고, 다 받아 둔 파일은 다시
         받지 않습니다. 서버가 이어받기를 받지 않으면 처음부터 받습니다.
       - 크기가 릴리스에 적힌 값과 다르면 해시를 셈하기 전에 버립니다.
       - `github.com`·`api.github.com`·`release-assets.githubusercontent.com`·
         `objects.githubusercontent.com`·`www.gyan.dev` 의 HTTPS 만 받습니다. 리다이렉트
         한 걸음마다, 그리고 끝 주소도 봅니다.     - **저장된 목록** (`tools\.ffmpeg_releases.json`)
       - 읽을 때마다 합친 목록(태그·파일 이름·크기·해시·확인한 출처)을 저장합니다.
       - API·gh·git 이 **모두 막히면** 7일 안에 저장한 목록으로 후보를 정합니다
         ("저장된 목록 — 날짜" 로 표시). 7일이 지난 목록은 쓰지 않습니다.
       - 같은 버전의 해시가 저장된 것과 다르면 "릴리스 파일이 바뀜" 으로 그 버전을 쓰지
         않습니다. 저장된 해시는 덮어쓰지 않으니, 정말 바뀐 것이 맞으면 이 파일을 지우세요.
       - USB 로 가져온 zip(`tools` 에 넣거나 `--from-zip`)은 **파일 이름으로** 저장된 해시를
         찾아 확인합니다. 이름이 목록에 없으면 기능 확인으로만 판단합니다.     - **시도 이력** (`tools\.ffmpeg_install.json`, 최근 20회): 출처별 결과, 시도한 버전과 이유,
       결과를 남기고, 같은 실패를 되풀이하지 않게 씁니다. `--history` 로 볼 수 있습니다.
       - API 한도 초과: 기록된 '풀리는 시각'까지 API 를 부르지 않습니다.
       - 같은 출처가 **3번 연속** 막힘·연결 실패·gh 확인 실패: 24시간 동안 건너뜁니다
         ('gh 없음'·'로그인 안 됨'·'git 없음' 은 세지 않음). 다만 세 출처가 모두 건너뛰게 되면
         할 수 있는 게 없으므로, 시각이 정확한 API 한도만 지키고 나머지는 다시 시도합니다.
       - 녹화에 필요한 기능이 없던 빌드는 같은 해시를 다시 받지 않습니다.
       - `--retry-skipped` 는 이 건너뛰기를 모두 무시하고 다시 시도합니다.     - **한도**: API 한도를 다 쓰면(시간당 60회) 다시 시도하지 않고 "HH:MM 에 풀림" 을
       보여 준 뒤 gh·git 으로 진행합니다. 한도가 남았는데 403 이면 막힌 망으로 봅니다.
   - `python -m syncnote.ffmpeg_fetch --dry-run` 으로 출처별 결과와 시도할 순서만 볼 수 있습니다.
4. 다운로드 폴더(`%USERPROFILE%\Downloads`)의 `ffmpeg*.zip`.
5. 파이썬조차 없을 때만 winget → PowerShell 로 gyan.dev zip.

전부 실패하면 **시도한 순서와 각각의 이유**, 그리고 원인에 맞는 **다음 할 일 하나**를
출력합니다. 설치·점검 창에도 그 한 줄이 설치 단계 메모로 뜹니다.

| 원인 | 다음 할 일 |
|---|---|
| 디스크 부족 | 공간 비우고 다시 |
| 쓰던 ffmpeg 잠김 | 녹화·송출 끄고 다시 |
| 인터넷에 안 닿음 | 학교·회사망이면 `HTTPS_PROXY` 설정 (설정돼 있으면 그 주소 확인) |
| 해시가 계속 다름 | 다른 망에서, 또는 USB 로 zip 을 `tools` 에 |
| 그 밖 | 다른 PC 에서 `*-essentials_build.zip` 을 받아 USB 로 `tools` 에 |

가져온 zip 을 직접 지정할 수도 있습니다:
`python -m syncnote.ffmpeg_fetch --from-zip D:\ffmpeg-9.1-essentials_build.zip [--sha256 …]`

winget 은 PATH 를 바꾸지만 **이미 열린 창에는 반영되지 않습니다.** syncnote 는
winget 이 둔 위치(`%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe`)를 직접 찾으므로
창을 다시 열지 않아도 녹화됩니다. 터미널에서 `ffmpeg` 명령을 치려면 창을 새로 여세요.

둘 다 안 되면:

1. PowerShell 에서 `winget install Gyan.FFmpeg` 를 직접 실행하거나
2. <https://www.gyan.dev/ffmpeg/builds/> 에서
   `ffmpeg-release-essentials.zip` 을 받습니다.
   압축을 풀어 `<레포>\tools\ffmpeg\bin\ffmpeg.exe` 가 되도록 둡니다.
3. 또는 PATH 에 추가하거나, 환경변수 `FFMPEG` 에 전체 경로를 넣습니다.

```bat
ffmpeg -version
```

---

## 5. 설정 파일

```bat
copy config.example.toml config.toml
notepad config.toml
```

최소한 이것들은 고쳐야 합니다.

```toml
[github]
owner = "본인-계정"
repo  = "본인-저장소"      # private 이어야 합니다

[eclass]
username = "학번"
```

---

## 6. 자격증명

```bat
scripts\set_password.bat
```

Windows 자격 증명 관리자(DPAPI)에 저장됩니다. 파일에 평문으로 남지
않고, 다른 사용자 계정에서는 읽을 수 없습니다.

### GitHub 토큰 만들기

1. <https://github.com/settings/tokens> → **Generate new token (classic)**
2. 범위는 **`repo` 하나만** 체크합니다.
3. 만료일을 설정하세요 (무기한은 피하세요).
4. 생성된 값을 `set_password.bat` 의 `[2]` 에 붙여 넣습니다.

Fine-grained token 을 쓴다면 대상 저장소에 대해
**Contents: Read and write** 권한이 필요합니다.

---

## 7. 오디오 장치 확인

```bat
scripts\devices.bat
```

`[loopback]` 으로 표시된 이름을 `config.toml` 의
`recording.audio.system_device` 에 적습니다. 부분 일치로 찾으므로
전체를 적지 않아도 됩니다.

---

## 8. 점검

```bat
scripts\doctor.bat
```

`[X]` 가 하나도 없어야 합니다.

---

## 9. 1분 시험 녹화

**자동 실행을 걸기 전에 반드시 하세요.**

```bat
scripts\record_now.bat -m 1 --no-upload
```

생성된 `.mkv` 를 열어 **화면이 보이고 소리가 들리는지** 확인합니다.
VLC 에서 `오디오 > 오디오 트랙` 을 열면 트랙 2개가 보여야 합니다.

---

## 10. 전사 모델 미리 받기

인터넷이 되는 곳에서 한 번 돌려 두세요.

```bat
.venv\Scripts\python -m syncnote transcribe warmup
```

---

## 11. 자동 실행 등록

**관리자 권한 PowerShell** 에서:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```

옵션:

```powershell
# Eclass 수집 주기를 6시간으로
... -EclassIntervalHours 6

# 시간표 녹화는 빼고 등록
... -SkipSchedule

# 전부 삭제
... -Remove
```

등록 후 작업 스케줄러에서 `Sync Note` 폴더를 열어 각 작업을
마우스 오른쪽 클릭 → **실행** 으로 한 번씩 확인하세요.

---

## 다른 PC 에 옮기기

```bat
git clone <저장소>
cd <폴더>
scripts\setup.bat
scripts\set_password.bat
```

`config.toml` 은 git 에 올라가지 않으므로 다시 만들어야 합니다.
여러 PC 에서 같은 설정을 쓰려면 별도 위치에 두고 환경변수로 가리키세요.

```bat
set SYNCNOTE_CONFIG=D:\OneDrive\설정\syncnote.toml
```

> 설정 파일만 클라우드에 두는 건 괜찮습니다.
> **데이터 폴더(`data_dir`)를 클라우드에 두지 마세요.**
