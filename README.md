# GYM-W-HALL-2F

국립군산대학교 **eClass 자료 자동 수집** + **화면/음성 녹화 및 전사**,
그리고 그 결과물을 **GitHub 릴리스에 자동 업로드**하는 도구 모음입니다.

프로그램(.exe)이 아니라 **소스코드**입니다. 모든 동작은
[`config.toml`](config.example.toml) 하나로 조정하고, 어느 PC에서든
`scripts\setup.bat` 하나로 환경이 갖춰집니다.

---

## 쓰기 전에 반드시 읽어야 할 것

이 도구는 **개인 학습용**을 전제로 만들어졌습니다.

- **저장소는 private 이어야 합니다.** 강의자료와 녹화본은 교수와 학교의
  저작물입니다. public 저장소에 올리면 사적 복제가 아니라 **공중송신**이
  되어 저작권 침해입니다. 이 도구는 막아 주지 않습니다.
- **녹화 전에 담당 교수에게 확인하세요.** 많은 학교가 수업 녹화·녹음을
  학칙으로 제한합니다.
- **시험·설문·토론 자동 수집은 기본으로 꺼져 있습니다.** 그 페이지는
  열람만으로도 LMS 에 응시/참여 기록이 남을 수 있어서, 켜더라도 목록
  제목만 읽고 상세 페이지에는 들어가지 않습니다. 자세한 내용은
  [docs/RISKS.md](docs/RISKS.md).
- **비밀번호는 코드나 설정 파일에 적지 않습니다.** Windows 자격 증명
  관리자(DPAPI)에 저장합니다.

---

## 빠른 시작 (Windows)

```bat
git clone https://github.com/<owner>/<repo>.git
cd <repo>
scripts\setup.bat
```

`setup.bat` 이 가상환경, 파이썬 패키지, Playwright 브라우저, ffmpeg 를
전부 준비하고 `config.toml` 을 만들어 줍니다.

그다음 순서대로:

```bat
scripts\set_password.bat      REM Eclass 비밀번호 / GitHub 토큰 저장
scripts\devices.bat           REM 오디오 장치 이름 확인 -> config.toml 에 반영
scripts\doctor.bat            REM 환경 점검
```

`config.toml` 에서 최소한 이것들은 고쳐야 합니다.

| 항목 | 설명 |
|---|---|
| `github.owner` / `github.repo` | 결과물을 올릴 **private** 저장소 |
| `eclass.username` | 학번 |
| `recording.timetable` | 시간표 (자동 녹화를 쓸 때) |
| `recording.audio.system_device` | `devices.bat` 에 나온 루프백 장치 이름 |

마지막으로 자동 실행을 걸려면 **관리자 권한 PowerShell** 에서:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1
```

---

## 할 수 있는 일

### 1. eClass 자료 수집

```bat
scripts\eclass_sync.bat
```

수강 중인 과목을 돌면서 아래를 받아 `data\eclass\<학기>\<과목>\<메뉴>\`
아래에 정리하고, 새로 생긴 것만 릴리스에 올립니다.

| 메뉴 | 기본값 | 저장 형태 |
|---|---|---|
| 강의 목록 / 교안 | 켬 | 마크다운 |
| 강의계획서 | 켬 | 마크다운 |
| 과목 정보 | 켬 | 마크다운 |
| 공지사항 | 켬 | 마크다운 + 첨부파일 |
| 질의응답 | 켬 | 마크다운 + 첨부파일 |
| 강의자료실 | 켬 | 마크다운 + 첨부파일 |
| 과제 (명세만) | 켬 | 마크다운 + 첨부파일 |
| 출석 | **끔** | 마크다운 (민감정보) |
| 성적 | **끔** | 마크다운 (민감정보) |
| 팀활동 | **끔** | 마크다운 |
| 시험 · 설문 · 토론 | **끔** | 목록 제목만 ([위험](docs/RISKS.md)) |

"생길 때마다"는 **작업 스케줄러가 3시간마다** 호출하는 방식으로
구현돼 있습니다. 이미 받은 글과 파일은 해시로 걸러 다시 받지 않습니다.

> **PC 를 켜 두지 않아도 수집하게 할 수 있습니다.**
> `.github/workflows/eclass-sync.yml` 을 쓰면 GitHub 서버에서 하루 세 번
> 돌아갑니다. 대신 학교 계정 비밀번호를 GitHub Secrets 에 맡겨야 하고,
> 학교가 해외 IP 를 막았다면 동작하지 않습니다. 장단점과 설정은
> [docs/CLOUD_SYNC.md](docs/CLOUD_SYNC.md) 에 있습니다.

### 2. 화면 녹화 + 음성 녹음

```bat
scripts\record_now.bat                  REM 지금 바로
scripts\record_now.bat -l 자료구조 -m 90 REM 이름과 길이 지정
```

- **화면**: ffmpeg (`gdigrab`)
- **시스템 소리**: WASAPI 루프백 — 온라인 강의 소리가 그대로 들어갑니다.
  **가상 오디오 드라이버 설치가 필요 없습니다.**
- **마이크**: 별도 트랙
- 결과는 `.mkv` 한 파일에 **오디오 트랙 2개**로 들어가므로 나중에 분리할
  수 있습니다.
- 기본 30분 단위로 끊어서 저장합니다 → 2GiB 제한 회피, 전원이 꺼져도
  앞부분은 보존.

실행 방식은 네 가지가 모두 준비돼 있습니다.

| 방식 | 명령 | 쓸 만한 때 |
|---|---|---|
| 수동 | `record_now.bat` | **가장 확실합니다.** 들고 다니는 노트북에 적합 |
| 시간표 | `record_schedule.bat` | 고정된 시간표, 늘 같은 자리 |
| 프로그램 감지 | `record_watch.bat` | Zoom/Webex 온라인 강의 |
| 터미널 | `.venv\Scripts\python -m gymhall record start` | 직접 호출 |

#### 소리만 — 터미널 녹음기 (FFmpeg · SoX · arecord)

화면 없이 소리만 받는 명령줄 녹음기도 있습니다. **macOS · Linux 에서도**
돌고, cron · 작업 스케줄러에 그대로 걸 수 있습니다.

```sh
python -m gymhall audio record -t 60 -f mp3   # FFmpeg: 정확히 60초
python -m gymhall audio record --vad --loop   # SoX: 소리가 날 때만, 계속
python -m gymhall audio record -b arecord     # Linux: 설치 없는 ALSA 기본 도구
python -m gymhall audio tools                 # 무엇이 깔려 있고 무엇이 실행될지
```

| 도구 | 지원 OS | 핵심 자동화 기능 | 쓸 만한 때 |
|---|---|---|---|
| FFmpeg | Windows, macOS, Linux | `-t` 로 정확한 길이, mp3 등 인코딩 | 예약 시간제 강의·회의 녹음 |
| SoX | Windows, macOS, Linux | `silence` 로 무음 감지 자동 시작·정지 | 소리 날 때만 기록하는 감시·메모 |
| arecord | Linux | 설치 불필요, 초경량 | 리눅스 서버·라즈베리파이 상시 녹음 |

파일 이름은 `record_20261116_090000.wav` 처럼 날짜·시각이 들어가 덮어쓰지
않고 쌓입니다. 예약은 Linux/macOS 는 `scripts/record_audio.sh` 를 crontab
에, Windows 는 `register_tasks.ps1 -AudioAt 09:00` 으로 겁니다.
설정은 `[recording.terminal]`, 자세한 내용은
[docs/TERMINAL_AUDIO.md](docs/TERMINAL_AUDIO.md).

#### 실시간 송출 — Tailscale 망 + 링크를 GitHub 이슈로

PC 소리(또는 마이크)를 실시간으로 내보내고 **같은 tailnet 의 기기만**
듣게 합니다. 링크가 생길 때마다(시작, 기기 주소 변경) 저장소 이슈로
올라가고, 끝나면 이슈에 길이·청취 수를 남기고 닫습니다.

```bat
scripts\stream_now.bat                     REM 또는 python -m gymhall stream start
python -m gymhall stream link              REM Tailscale 상태와 링크 미리 보기
```

- 오디오 서버: 내장 HTTP 서버(설치 없음) 또는 **Icecast**(오픈소스 스트리밍 서버)
- 노출: `serve`(tailnet 전용 HTTPS, 기본) / `direct` / `funnel`(인터넷 공개, 명시적 허용 필요)
- 접속 토큰, 청취자 상한, 최대 길이, 청취자 없으면 멈춤, 이슈 제목·본문·라벨·담당자·
  링크 변경 시 동작까지 `[stream]` 에서 전부 조정합니다.
- 이미 쓰고 있는 `tailscale serve/funnel` 포트는 건드리지 않습니다.

자세한 내용은 [docs/STREAMING.md](docs/STREAMING.md).

### 3. 음성 전사

```bat
scripts\transcribe_queue.bat
```

`faster-whisper` 로 **로컬에서** 전사합니다. 음성이 외부로 나가지 않습니다.

- **정밀 전사(기본)**: 녹화가 끝난 뒤 대기열에 쌓였다가 처리됩니다.
  저사양 노트북이면 충전 중에 돌리세요.
- **실시간 전사(기본 꺼짐)**: 녹화 중 임시 자막을 만듭니다.
  **저사양 PC에서는 켜지 마세요** — 녹화 프레임이 떨어집니다.
  결과는 임시본이고, 나중에 정밀본이 따로 생성됩니다.

**과목마다 언어를 따로 줄 수 있습니다.** 영어 수업을 `ko` 로 전사하면
Whisper 가 소리 나는 대로 한글을 뱉어("디스 이즈 어 바이너리 트리")
그 과목 전사본이 통째로 못 쓰게 됩니다. 시간표 항목에 `language = "en"`
을 넣거나 `--language en` 을 붙이세요. 한국어 설명에 영어 용어가 많이
섞이는 과목은 `"ko+en"` 으로 두면 둘 중에서만 고릅니다(`auto` 로 두면
그 구간에서 일본어·중국어로 오판하는 일이 있습니다).

**지어낸 문장은 걸러냅니다.** Whisper 는 유튜브 자막으로 학습돼서
무음 구간을 만나면 "시청해주셔서 감사합니다" 같은 상투어를 지어냅니다.
강의는 판서 시간·질문 대기·쉬는 시간처럼 무음이 길어 이게 특히 잘
생깁니다. 알려진 상투어, 모델이 스스로 무음이라 판단한 구간, 같은 문장
반복 루프를 제거합니다(`[transcribe.cleanup]` 에서 조정·해제 가능).

**예상 시간은 이 기기에서 실측한 값으로 말합니다.** 전사가 끝날 때마다
실제 처리 속도를 기록해 다음 예상에 반영합니다. 같은 오디오라도 기기와
그때의 CPU 부하에 따라 몇 배씩 차이 나기 때문에, 일반적인 수치를
적어 두는 것은 의미가 없습니다. 실측 전에는 "대략값" 이라고 분명히
밝힙니다.

```bat
scripts\transcribe_queue.bat
REM   오디오 합계 2시간 30분
REM   예상 1시간 12분 (이 기기 실측 4회 기준, small/cpu)
```

처음 한 번은 인터넷이 되는 곳에서 모델을 받아 두세요.

```bat
.venv\Scripts\python -m gymhall transcribe warmup
```

### 4. GitHub 릴리스 업로드

- 태그는 **월별로 하나**(`recordings-2026-11`)를 쓰고 에셋을 누적합니다.
  세션마다 릴리스를 만들면 태그가 수백 개로 불어납니다.
- **2GiB 가 넘는 파일은 자동으로 `.partNNN` 분할**되고, 복원용
  `*.parts.json` 과 합치기 명령이 함께 올라갑니다.
- 한글 파일명은 GitHub 가 망가뜨리므로 안전한 이름 + 해시로 바꿔 올리고,
  원래 이름은 에셋 label 에 보존합니다.

---

## 전체 명령

```
python -m gymhall doctor                 환경 점검
python -m gymhall devices                오디오 장치 목록
python -m gymhall config check           설정 검증
python -m gymhall secrets set eclass     비밀번호 저장
python -m gymhall eclass probe           페이지 구조 탐색(디버그)
python -m gymhall eclass sync            자료 수집 + 업로드
python -m gymhall record start           수동 녹화
python -m gymhall record schedule        시간표 대기
python -m gymhall record watch           프로그램 감지 대기
python -m gymhall audio record           소리만 녹음 (FFmpeg / SoX / arecord)
python -m gymhall audio tools            터미널 녹음 도구 점검
python -m gymhall stream start           실시간 송출 (Tailscale, 링크 -> 이슈)
python -m gymhall stream link            송출 링크·Tailscale 점검
python -m gymhall stream cleanup         남은 tailscale serve 끄기
python -m gymhall transcribe queue       대기열 전사
python -m gymhall transcribe warmup      모델 미리 받기
python -m gymhall upload <경로>          임의 파일 업로드
```

모든 명령에 `--dry-run`(업로드 흉내), `--no-upload`, `--log-level DEBUG`
를 붙일 수 있습니다.

---

## 폴더 구조

```
config.toml              ← 조정은 전부 여기서. git 에 올라가지 않습니다.
requirements.txt
scripts\                 ← .bat / .vbs / PowerShell
src\gymhall\
  config.py              설정 로드와 검증
  release.py             릴리스 업로드 + 2GiB 분할
  state.py               중복 방지 기록
  secrets.py             자격증명
  eclass\
    urls.py              eClass URL 지도 (menuId 포함)
    browser.py           Playwright 세션 + 로그인
    crawler.py           메뉴별 수집
    probe.py             구조 탐색
  recorder\
    video.py             ffmpeg 화면 캡처
    audio.py             WASAPI 루프백 + 마이크
    mux.py               합치기 + A/V 싱크 보정
    session.py           세그먼트 관리
    schedule.py          시간표
    watcher.py           프로그램 감지
    terminal.py          터미널 녹음기 (FFmpeg / SoX / arecord)
  stream\
    capture.py           소리 -> ffmpeg 인코딩 (mp3 / aac, Icecast)
    hub.py               내장 오디오 서버 (청취자 분배, 토큰)
    tailnet.py           Tailscale 상태, serve/funnel, 링크
    notify.py            링크 -> GitHub 이슈
    live.py              송출 세션
  transcribe\
    batch.py             정밀 전사 + 대기열
    realtime.py          실시간(임시) 전사
    cleanup.py           Whisper 환각 필터
    language.py          전사 언어 결정 (과목별 지정)
    perf.py              이 기기의 실측 전사 속도
tests\                  pytest. 네트워크/장치/계정 없이 돕니다
docs\
  SETUP.md               설치 상세
  CONFIG.md              설정 항목 설명
  RISKS.md               위험 메뉴와 법적 주의
  TROUBLESHOOTING.md     안 될 때
  TESTING.md             테스트와 개발 중 잡은 버그들
  CLOUD_SYNC.md          PC 없이 수집하기 (GitHub Actions)
  TERMINAL_AUDIO.md      소리만 녹음 + cron / 작업 스케줄러
  STREAMING.md           실시간 송출 (Tailscale + GitHub 이슈)
```

산출물은 저장소가 아니라 `%LOCALAPPDATA%\gym-w-hall-2f\` 에 쌓입니다
(`general.data_dir` 로 바꿀 수 있습니다).

---

## 알려진 한계

- **화면 녹화는 Windows 전용**입니다. 화면 캡처와 WASAPI 루프백이 그렇습니다.
  (코드는 Linux에서 `x11grab` 으로 돌아가지만 시험하지 않았습니다.)
  소리만이라면 `audio record`(터미널 녹음기)가 macOS · Linux 에서 돕니다.
- **세그먼트 사이에 1초 안팎의 공백**이 생깁니다. 강의에는 문제가 없지만
  연속 측정에는 맞지 않습니다.
- **학교가 LMS 를 개편하면 수집이 멈춥니다.** `eclass_probe.bat` 으로
  새 구조를 떠서 `src/gymhall/eclass/urls.py` 를 고쳐야 합니다.
- **작업 스케줄러 기반 자동 녹화는 놓칠 수 있습니다.** 노트북이 꺼져
  있거나 절전이면 그 수업은 그냥 사라집니다. 중요한 수업은 직접 시작하세요.
  (자료 수집은 GitHub Actions 로 이 문제를 피할 수 있지만, 녹화는
  화면과 오디오 장치가 필요해 PC 에서만 가능합니다.)
- 로그인 시도를 자동 재시도하지 않습니다. 비밀번호를 틀리면 한 번만
  시도하고 멈춥니다 — 학교 계정 잠금을 피하기 위해서입니다.

---

## 개발

```bash
pip install pytest numpy requests
python -m pytest
```

247개 테스트가 네트워크, 오디오 장치, 학교 계정 없이 전부 돌아갑니다.
`ffmpeg` 가 있으면 실제 합치기 테스트가, Playwright + Chromium 이 있으면
실제 브라우저로 로그인 폼을 조작하는 테스트가 추가로 돕니다.

CI 는 **Python 3.10 / 3.11 / 3.12 / 3.13 에서 전부** 돌립니다.
`threading.Thread._stop` 이 3.13 에서 사라진 탓에, 3.13 에서만 멀쩡하고
3.11/3.12 에서 녹화가 깨지는 버그가 실제로 있었기 때문입니다.
자세한 내용과 개발 중 잡은 다른 버그들은
[docs/TESTING.md](docs/TESTING.md) 에 있습니다.

---

## 강의실 PC 에 배포하기 전에

**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 를 한 번 훑으세요.** 전부
실제로 당한 것들입니다. 특히 **마이크** — 기본 마이크가 가상 장치로
잡혀 수업 내내 비트 단위 0 이 녹음된 적이 있습니다. 끝나고 로그를
봐야 알았습니다.

---

## 문제가 생기면

1. `scripts\doctor.bat` — 대부분 여기서 원인이 나옵니다.
2. 로그 파일 — `doctor.bat` 이 알려 주는 데이터 폴더의 `logs\gymhall.log`.
   작업 스케줄러로 돌리면 창이 안 뜨므로 **로그가 유일한 단서**입니다.
3. [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)
