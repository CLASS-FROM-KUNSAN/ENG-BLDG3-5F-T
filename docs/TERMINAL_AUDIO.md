# 터미널 녹음기 — FFmpeg · SoX · arecord

`record start` 는 화면과 소리를 함께 받는 **Windows 전용** 녹화기입니다.
터미널 녹음기는 그 바깥을 맡습니다.

- **소리만** 받습니다. 화면은 받지 않습니다.
- **macOS · Linux** 에서도 돕니다.
- 명령줄 한 줄이라 **cron · 작업 스케줄러**에 그대로 걸 수 있습니다.
- 셋 중 **SoX** 는 소리가 날 때만 녹음하는 **소리 감지(VAD)** 를 합니다.

```sh
python -m gymhall audio tools                 # 설치된 도구와 실행될 명령 보기
python -m gymhall audio record                # [recording.terminal] 설정대로 한 번
python -m gymhall audio record -t 60 -f mp3   # 60초, mp3
python -m gymhall audio record --vad          # 소리가 나면 시작, 3초 조용하면 끝 (SoX)
python -m gymhall audio record --vad --loop   # 위를 계속 반복 (상시 감시)
python -m gymhall audio record --print        # 실행하지 않고 명령만 출력
```

`--print` 가 내놓는 줄은 그 도구의 순수한 명령이라, 이 프로젝트 없이
셸에 붙여 넣어도 그대로 돕니다.

---

## 세 도구 비교

| 도구 | 지원 OS | 핵심 자동화 기능 | 쓸 만한 때 |
|---|---|---|---|
| **FFmpeg** | Windows, macOS, Linux | `-t` 로 정확한 녹음 길이, 다양한 포맷 인코딩 | 예약 시간제 강의·회의 녹음 |
| **SoX** | Windows, macOS, Linux | `silence` 로 무음 감지 자동 시작·정지 | 소리가 날 때만 기록하는 감시·음성 메모 |
| **arecord** | Linux | 설치 불필요, 초경량 백그라운드 실행 | 리눅스 서버·라즈베리파이 상시 녹음 |

`backend = "auto"`(기본)는 **ffmpeg → sox → arecord(리눅스만)** 순으로
있는 것을 씁니다. `--vad` 를 주면 SoX 로 갑니다 — 소리 감지는 SoX 만
됩니다. 특정 도구를 쓰려면 `-b ffmpeg` / `-b sox` / `-b arecord`.

### 1. FFmpeg

영상·오디오 처리의 표준 도구입니다. 이 프로젝트가 이미 화면 녹화에
쓰고 있어서(`setup.bat` 이 받아 줍니다) 따로 깔 것이 없습니다.
`-t` 로 길이를 정확히 끊고 스스로 끝납니다.

| OS | 만들어지는 명령 |
|---|---|
| macOS | `ffmpeg -f avfoundation -i ":0" -t 60 output.mp3` |
| Linux | `ffmpeg -f pulse -i default -t 60 output.mp3` |
| Windows | `ffmpeg -f dshow -i "audio=<장치 이름>" -t 60 output.mp3` |

(실제 명령에는 `-hide_banner -nostdin`, 샘플레이트·채널, 코덱이 더 붙습니다.)

Windows 의 dshow 는 장치 이름이 꼭 필요합니다.

```bat
ffmpeg -hide_banner -list_devices true -f dshow -i dummy
```

에 나온 이름을 `device` 에 적으세요. macOS 는 번호입니다
(`ffmpeg -f avfoundation -list_devices true -i ""`).

### 2. SoX (Sound eXchange)

소리 감지 기능이 내장돼 있습니다. 문서에 흔히 나오는 이 한 줄이

```sh
rec output.wav silence 1 0.1 3% 1 3.0 3%
```

`--vad` 그대로입니다. 앞의 `1 0.1 3%` 는 *3% 이상 소리가 0.1초
이어지면 시작*, 뒤의 `1 3.0 3%` 는 *3% 아래로 3초 이어지면 끝* 입니다.
(`rec` 은 `sox -d` 의 다른 이름이라 여기서는 `sox -d` 로 부릅니다.)

```toml
[recording.terminal]
vad           = true
vad_threshold = "3%"     # 시끄러운 방이면 올리고, 작은 목소리면 내리세요. "-40d" 처럼 dB 도 됩니다
vad_start_sec = 0.1
vad_stop_sec  = 3.0      # 강의는 말 사이가 길어 10 정도가 낫습니다
seconds       = 3600     # vad 일 때는 '최대' 길이 (trim 0 3600)
```

아무 소리도 안 나면 SoX 는 영원히 기다립니다. 그래서 `seconds` 를
`trim` 상한으로 붙입니다(소리가 난 뒤부터 잽니다).

설치:

| OS | 명령 |
|---|---|
| macOS | `brew install sox` |
| Debian/Ubuntu | `sudo apt install sox libsox-fmt-pulse` |
| Windows | <https://sourceforge.net/projects/sox/> 설치 후 PATH 에 추가 (또는 환경변수 `SOX` 에 sox.exe 경로) |

SoX 는 배포판에 따라 mp3 를 못 써서 **wav / flac 만** 받습니다.

### 3. arecord (Linux 전용)

ALSA 의 기본 도구라 대부분의 배포판에 이미 있습니다(`alsa-utils`).
자원을 거의 쓰지 않아 라즈베리파이나 서버에서 상시 녹음할 때 좋습니다.

```sh
arecord -f cd -d 60 output.wav           # CD 음질로 60초
python -m gymhall audio record -b arecord -t 60
```

wav 만 쓰고, `-d` 는 정수 초만 받습니다(1.2초는 2초로 올림합니다).
장치는 `arecord -L` 로 보고 `device = "plughw:1,0"` 처럼 적습니다.

---

## 시스템 소리(스피커로 나가는 소리)

`source = "system"` 은 OS 마다 사정이 다릅니다.

| OS | 어떻게 되나 |
|---|---|
| Linux (PulseAudio/PipeWire) | `@DEFAULT_MONITOR@` 로 바로 됩니다. ffmpeg·sox 모두 |
| macOS | OS 가 내주지 않습니다. [BlackHole](https://github.com/ExistentialAudio/BlackHole) 같은 가상 장치를 깔고 `device` 에 그 번호/이름을 적으세요 |
| Windows | ffmpeg·sox 의 입력은 루프백을 모릅니다. **`record start` 를 쓰세요** (WASAPI 루프백, 설치 불필요) |
| arecord | snd-aloop 같은 ALSA 루프백 장치를 `device` 에 줘야 합니다 |

안 되는 조합은 도구를 실행하기 전에 막고, 무엇을 하라고 알려 줍니다.

---

## 파일 이름 — 덮어쓰지 않고 쌓기

```toml
filename = "record_%Y%m%d_%H%M%S"   # -> record_20261116_090000.wav
```

셸의 `record_$(date +%Y%m%d_%H%M%S).mp3` 와 같은 일을 하지만,
`%` 를 특별하게 다루는 Windows 배치 파일·cron 에서도 그대로 동작합니다
(cron 에서 `date +%Y` 를 쓰려면 `\%` 로 이스케이프해야 합니다).
같은 초에 두 번 돌면 ` (2)` 가 붙습니다.

저장 위치는 `out_dir`(또는 `-o`)이고, 비우면
`<데이터 폴더>/recordings/<날짜>/terminal/` 입니다.

---

## 완전 자동화

### Linux · macOS — cron

```sh
crontab -e
```

```cron
# 평일 09:00 에 50분 녹음
0 9 * * 1-5  /path/to/repo/scripts/record_audio.sh -t 3000 >/dev/null 2>&1

# 부팅하면 소리 감지 상시 녹음 (SoX)
@reboot      /path/to/repo/scripts/record_audio.sh --vad --loop >/dev/null 2>&1
```

`scripts/record_audio.sh` 가 `PYTHONPATH`, `.venv`, `config.toml`,
PulseAudio 용 `XDG_RUNTIME_DIR` 을 맞춰 줍니다. cron 의 PATH 는
`/usr/bin:/bin` 뿐이라 brew 로 깐 ffmpeg·sox 가 안 보이면 crontab 맨
위에 `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin` 을 넣으세요.

macOS 는 cron 이 마이크를 쓰려면 *시스템 설정 > 개인정보 보호 및
보안 > 마이크* 에서 터미널(또는 cron)을 허용해야 합니다. 허용하지 않으면
파일은 생기는데 소리가 전부 0 입니다.

### Windows — 작업 스케줄러

관리자 PowerShell 에서:

```powershell
# 매일 09:00 에 [recording.terminal] 설정대로
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -AudioAt 09:00

# 길이와 형식을 덮어쓰기
powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -AudioAt 09:00 -AudioArgs "-t","3000","-f","mp3"
```

`TerminalAudio` 작업이 `hidden.vbs` 를 거쳐 `record_audio.bat` 을
**창 없이** 돌립니다. `-AudioAt` 을 주지 않으면 이 작업은 만들지 않습니다.
요일을 고르거나 여러 시각을 걸려면 작업 스케줄러에서 트리거를 더 하세요.

직접 걸고 싶다면:

```bat
schtasks /Create /TN "GYM-W-HALL-2F\TerminalAudio" /SC DAILY /ST 09:00 ^
  /TR "wscript.exe \"C:\path\repo\scripts\hidden.vbs\" \"C:\path\repo\scripts\record_audio.bat\" -t 3000"
```

창이 없으니 오류는 로그(`<데이터 폴더>\logs\`)에서 보세요.

---

## 무엇을 하지 않나

- **전사 대기열에 넣지 않고, 릴리스에 올리지 않습니다.** 받은 파일은
  로컬에 남습니다. 올리려면 `python -m gymhall upload <파일>` 을 쓰세요.
- **`record start` 와 자리를 다투지 않습니다.** 녹화 잠금을 잡지 않으므로
  둘을 동시에 돌리면 같은 마이크를 두 프로그램이 엽니다.
  (Windows 는 대개 둘 다 받고, 일부 USB 장치는 하나만 받습니다.)
