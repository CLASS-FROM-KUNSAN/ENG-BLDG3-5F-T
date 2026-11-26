# scripts 폴더 안내

Windows 용 `.bat` · PowerShell `.ps1` · macOS/Linux 용 `.sh` 를 모아 둔 곳입니다. 각 파일이
무엇을 하는지와, 스크립트를 고칠 때 지켜야 하는 규칙을 적습니다.

## 규칙 1 — `.bat` 은 ASCII 만 (한글은 `scripts\msg\*.txt`)

**모든 `.bat` 파일에는 ASCII 글자만 씁니다.** 주석은 영어로, 사용자에게 보여 줄 한글 안내는
`scripts\msg\<이름>.txt`(UTF-8, BOM 없음)에 두고 이렇게 보여 줍니다.

```bat
call "%~dp0_say.bat" setup_1
```

`_say.bat` 은 그 파일을 `type` 으로 그대로 화면에 보냅니다.

**왜:** cmd 는 배치 파일에 한글(여러 바이트 글자)이 있으면 줄 위치를 잘못 계산해, 주석 조각을
명령으로 실행합니다. 2026-11-23 실기(관리자 권한이 아닌 cmd)에서:

```
'━硫?臾듭?'은(는) 내부 또는 외부 명령, 실행할 수 있는 프로그램, 또는 배치 파일이 아닙니다.
```

재현해 보니 한글 주석이 있는 `_common.bat` 이 `_find_python.bat` 을 `call` 하고 돌아올 때,
cp949 창과 UTF-8 창 **모두에서** 이전 줄의 한글 조각이 명령으로 실행됐습니다
(`'바이트코드가' is not recognized ...` — 예전에 강의실 PC 에서 본 것과 같은 오류).
같은 파일에서 한글 주석만 빼면 사라졌고, `call` 을 한글보다 앞에 두어도 사라졌습니다.
어떤 조합에서 터지는지는 cmd 내부 동작이라 미리 다 알 수 없으므로, **한글을 `.bat` 에서
아예 빼는 것**으로 막습니다. `type` 은 파일 내용을 배치 줄로 해석하지 않고 화면에 그대로
보내므로 이 버그와 상관없습니다.

- 메시지 끝에 줄바꿈이 없으면 다음 `echo` 가 같은 줄에 이어집니다. 경로·변수처럼 ASCII 인
  부분은 이렇게 `echo` 로 붙입니다.

  ```bat
  call "%~dp0_say.bat" setup_python
  echo %PYBASE%  [%PYFROM%]
  ```
- `_say.bat` 은 `_common.bat` 뒤에 부릅니다. `_common.bat` 이 맨 끝에서 콘솔을 UTF-8
  (`chcp 65001`)로 바꿔야 한글이 제대로 보입니다.
- `tests/test_scripts.py` 가 지킵니다: `.bat` 에 ASCII 가 아닌 글자가 있으면, `_say` 로 부른
  메시지 파일이 없으면, 메시지 파일에 BOM 이 있으면 실패합니다.

## 규칙 2 — 파이썬은 `_find_python` 에서만

자세한 내용은 [설치 상세의 '파이썬 실행 규칙'](SETUP.md#파이썬-실행-규칙-모든-스크립트가-지킵니다).
`python` / `py` / `python3` 를 직접 부르지 않고 `%PY%` / `$PY` 만 씁니다.

## 규칙 3 — 실행 정책 확인은 정규 과정 (`exec_policy.ps1`)

모든 스크립트는 먼저 PowerShell 실행 정책을 확인합니다. `.bat` 은 `_common.bat` 의 마지막 단계에서,
`.ps1` 은 맨 앞에서, 작업 스케줄러 자동 등록(`src/syncnote/tasks.py`)은 등록 전에 거칩니다.

1. `Get-ExecutionPolicy -List` 로 영구 정책을 봅니다. 이 확인은 `-ExecutionPolicy Bypass` 로 실행되므로
   Process 범위는 빼고 봅니다. 그대로 `Get-ExecutionPolicy` 를 부르면 늘 Bypass 로 나옵니다.
2. `.ps1` 을 실행할 수 없는 정책(Restricted·AllSigned·Undefined)이면
   `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` 로 **영구** 설정합니다.
   - 레지스트리에 저장되어 재부팅해도 유지되고, 관리자 권한이 필요 없습니다.
   - 관리자 권한으로 실행 중이면 LocalMachine 도 켭니다.
   - Unrestricted 가 아니라 RemoteSigned 입니다. 인터넷에서 받은 스크립트는 서명이 있어야 실행됩니다.
3. 인터넷에서 받은 표시(MOTW)가 붙은 `scripts\*.ps1` 은 차단을 풉니다.
4. 그룹 정책(MachinePolicy·UserPolicy)으로 막혀 있으면 바꿀 수 없습니다. 알리기만 하고, 우리 작업은
   `-ExecutionPolicy Bypass` 로 부르므로 그대로 돕니다.
5. 무슨 일이 있어도 종료 코드는 0입니다. 정책 문제로 스크립트나 녹화를 멈추지 않습니다.

- 정상이면 아무것도 출력하지 않습니다. `setup.bat` 과 `doctor.bat` 은 현재 정책을 한 줄 보여 줍니다
  (`SN_POLICY_SHOW=1`). `doctor` 에는 점검 항목으로도 나옵니다.
- `activity_host.bat` 은 표준입출력이 브라우저와의 통신용이라, 입력을 닫고 출력 없이 실행합니다.
- 이 단계를 부를 때만 `PSModulePath` 를 비웁니다. PowerShell 7 창에서 `.bat` 을 실행하면 이 값이
  PowerShell 7 의 모듈을 가리켜, Windows PowerShell 5.1 이 `Get-ExecutionPolicy` 를 불러오지 못했습니다.
  비우면 5.1 이 자기 기본값을 다시 만듭니다.

## 공통 파일

| 파일 | 하는 일 |
|---|---|
| `_common.bat` | 모든 `.bat` 이 맨 처음 `call` 합니다. 파이썬 관련 환경 변수(UTF-8, .pyc 안 남김), `REPO`·`PYTHONPATH`, 파이썬 고르기(`_find_python.bat`), 동봉 ffmpeg 를 PATH 에, `SYNCNOTE_CONFIG` 위치, 마지막으로 콘솔을 UTF-8 로. |
| `_find_python.bat` | 파이썬을 '실제로 실행되고 3.10 이상인지' 로 고름. `SYNCNOTE_PYTHON` → `python` → `py -3` → `python3` → `py`. 아무것도 출력하지 않음. |
| `_find_python.sh` | macOS·Linux 판. `SYNCNOTE_PYTHON` → `python3` → `python` → `py`. |
| `_no_python.bat` | 파이썬을 어디서도 못 찾았을 때 `%PY%` 가 가리키는 안내. |
| `_say.bat` | `msg\<이름>.txt` 를 화면에 보여 줌(규칙 1). |
| `hidden.vbs` | 작업 스케줄러가 `.bat` 을 창 없이 띄울 때 씀. |

## 사람이 실행하는 것

| 파일 | 하는 일 |
|---|---|
| `setup.bat` | 최초 1회 설치(더블클릭). 가상환경 → 패키지 → Playwright 브라우저(학교망 인증서 검사면 Windows 인증서로 다시) → ffmpeg·SoX → Tailscale → config.toml → 마이크 권한. 실행되지 않는 `.venv` 는 지우지 않고 `.venv.broken-<번호>` 로 옮김. |
| `timetable_edit.bat` | 시간표 편집기(GUI). PySide6 가 없으면 그 자리에서 설치를 물음. |
| `set_password.bat` | 비밀번호·토큰을 Windows 자격 증명 관리자에 저장(화면·파일에 안 남음). `set_password.bat 1` 처럼 번호를 주면 메뉴 없이 그 항목. |
| `doctor.bat` | 환경 점검. |
| `devices.bat` | 오디오 장치 목록(이름을 config.toml 에 적음). |
| `record_now.bat` | 지금 바로 녹화. 창을 닫거나 Ctrl+C 면 안전하게 마무리. |
| `stream_now.bat` | 실시간 오디오 송출(Tailscale). Ctrl+C 면 멈추고 serve 를 끄고 이슈를 닫음. |
| `transcribe_queue.bat` | 밀린 전사를 한 번에(충전기에 꽂고). |
| `eclass_probe.bat` | eClass 화면 구조가 바뀌어 수집이 안 될 때. 결과는 데이터 폴더 `state\probe`. |
| `open_data_folder.bat` | 녹화·자료가 저장된 폴더 열기. |
| `install_ffmpeg.bat` | ffmpeg 설치(setup 이 부름). 찾기 → 가져온 zip → pip → winget → 릴리스 목록(롤백) → 다운로드 폴더 zip, 파이썬이 없으면 winget → PowerShell zip. |
| `audio_check.bat` | 소리 입출력 단계별 점검: 무음 → 마이크에 말하기 → 증폭해서 들어 보기 → 톤 → 잡음 → 요약. 마이크 소리가 실제로 들어오는 장치를 찾습니다(docs/TROUBLESHOOTING.md). |
| `release.bat` | `vX.Y.Z` 태그를 만들어 푸시(GitHub Actions 가 릴리스). 이미 있는 태그·형식이 틀린 태그는 거부. |

## 작업 스케줄러가 실행하는 것 (`register_tasks.ps1` 이 등록)

등록은 자동입니다. 사람이 실행한 `setup.bat`·`doctor.bat`·시간표 편집기 저장·`register_tasks.ps1` 은
새로 등록하거나 바꿀 것이 있을 때 **'등록할까요? (Y/N)'** 를 먼저 묻습니다. 필요하면 UAC 도 띄웁니다.
그리고 모든
`syncnote` 명령의 시작(뒤에서, 몇 시간에 한 번)에서 빠진 작업·옛 폴더를 가리키는 작업·꺼진
작업을 찾아 묻지 않고 `register_tasks.ps1 -Yes` 를 실행합니다(`src/syncnote/tasks.py`). 물어볼 사람이 없어서입니다. `doctor.bat --json`
(편집기가 읽음)에서는 출력을 섞지 않으려고 등록하지 않습니다.

| 파일 | 하는 일 |
|---|---|
| `record_schedule.bat` | 로그온 시 시작, 시간표대로 녹화. |
| `record_watch.bat` | 로그온 시 시작, Zoom 등 지정 프로그램이 뜨면 녹화. |
| `record_guard.bat` | 녹화 지킴이: 녹화 중이 아닐 때 녹화 파일을 확인하고 못 올린 것을 올림. |
| `eclass_sync.bat` | 주기적으로 eClass 수집·업로드. |
| `transcribe_run.bat` | 야간 무인 전사. |
| `home_backup.bat` / `home_prune.bat` | 홈 백업 / 백업이 확인된 파일 정리(설정에서 켜야 동작). |
| `record_audio.bat` | (선택, `-AudioAt`) 매일 정해진 시각에 소리만 녹음. 파일 이름에 날짜·시각. |
| `stream_schedule.bat` | (선택, `-Stream`) 시간표대로 수업마다 송출. |

## 그 밖

- `register_tasks.ps1`, `register_host.ps1` — 작업·브라우저 확장 호스트 등록. 한글이 있으므로
  UTF-8 BOM 을 둡니다(Windows PowerShell 5.1 이 BOM 없으면 cp949 로 읽음).
- `activity_host.bat` — 브라우저 확장이 띄우는 네이티브 메시징 호스트. 표준출력은 통신용이라
  아무것도 출력하지 않습니다(`_common.bat` 을 부르지 않음).
- `record_audio.sh`, `stream_now.sh` — macOS·Linux 용.
