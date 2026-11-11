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

자동 설치가 안 되면:

1. <https://www.gyan.dev/ffmpeg/builds/> 에서
   `ffmpeg-release-essentials.zip` 을 받습니다.
2. 압축을 풀어 `<레포>\tools\ffmpeg\bin\ffmpeg.exe` 가 되도록 둡니다.
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
.venv\Scripts\python -m gymhall transcribe warmup
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

등록 후 작업 스케줄러에서 `GYM-W-HALL-2F` 폴더를 열어 각 작업을
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
set GYMHALL_CONFIG=D:\OneDrive\설정\gymhall.toml
```

> 설정 파일만 클라우드에 두는 건 괜찮습니다.
> **데이터 폴더(`data_dir`)를 클라우드에 두지 마세요.**
