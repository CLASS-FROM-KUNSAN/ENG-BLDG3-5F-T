# 실시간 오디오 송출 — Tailscale + GitHub 이슈

PC 에서 나는 소리(또는 마이크)를 실시간으로 내보내고, **같은 Tailscale
망(tailnet)** 에 들어와 있는 기기만 듣게 합니다. 링크가 생길 때마다
저장소 이슈로 올라가므로, 듣는 사람은 이슈 알림만 보고 들어오면 됩니다.

```
소리 ─▶ ffmpeg (mp3/aac) ─▶ 내장 오디오 서버 (127.0.0.1:8765)
                               │
                       tailscale serve (HTTPS, tailnet 전용)
                               │
     https://<기기>.<tailnet>.ts.net/live?t=<토큰>  ──▶  GitHub 이슈
```

```sh
python -m gymhall stream link       # 먼저: Tailscale 상태와 만들어질 링크 점검
python -m gymhall stream start      # 송출 시작 (Ctrl+C 로 멈춤)
python -m gymhall stream schedule   # 시간표대로 수업마다 송출
python -m gymhall stream start --dry-run   # 이슈는 만들지 않고 내용만 출력
python -m gymhall stream cleanup    # 강제 종료로 남은 tailscale serve 끄기
```

Windows 는 `scripts\stream_now.bat`, macOS·Linux 는 `scripts/stream_now.sh`.

> **먼저 읽으세요.** 강의 소리는 교수와 학교의 저작물입니다. 함께 듣는
> 사람은 같은 tailnet 의 본인 기기나 허락받은 사람으로 한정하세요.
> `funnel`(인터넷 공개)이나 public 저장소에 링크를 올리는 것은 공중송신이
> 됩니다. 그래서 둘 다 기본으로 막혀 있고, 설정에 직접 적어야 풀립니다.

---

## 준비

1. **Tailscale** 설치 후 로그인 (`tailscale up`). 듣는 기기도 같은 tailnet 에.
2. 관리 콘솔(DNS 탭)에서 **MagicDNS** 와 **HTTPS Certificates** 켜기 —
   `serve` 모드에 필요합니다.
3. **GitHub 토큰** (`python -m gymhall secrets set github`). 이슈 쓰기 권한이
   있어야 합니다(classic: `repo`, fine-grained: Issues read/write).
4. `python -m gymhall stream link` 로 점검.

---

## 오디오 서버 고르기 — `[stream] server`

| 값 | 무엇 | 설치 | 접속 토큰 |
|---|---|---|---|
| `builtin` (기본) | 파이썬 표준 라이브러리 HTTP 서버. 청취자마다 큐를 따로 두고 느린 청취자만 끊음 | 없음 | 됨 |
| `icecast` | [Icecast](https://icecast.org/) — 오픈소스 스트리밍 서버. ffmpeg 가 `icecast://` 로 밀어 넣음 | Icecast 를 따로 띄움 + `secrets set icecast` | Icecast 쪽 인증 사용 |

청취자가 많거나 Icecast 의 기능(통계, 중계, 대체 마운트)을 쓰고 싶으면
`icecast`, 아니면 `builtin` 으로 충분합니다.

`builtin` 의 주소:

| 경로 | 내용 |
|---|---|
| `/live?t=…` | 오디오 스트림 (`path` 로 바꿀 수 있음) |
| `/?t=…` | 재생 페이지 (`player_page = false` 로 끔) |
| `/status?t=…` | 청취자 수 JSON |

코덱은 `mp3`(기본)와 `aac`(ADTS) 입니다. 둘 다 중간에 들어온 청취자도
바로 재생됩니다. 말소리는 `bitrate_kbps = 64` 로도 충분합니다.

## 소리 받기 — `source`, `capture`

| `capture` | 어떻게 | 언제 |
|---|---|---|
| `soundcard` | 녹화기와 같은 장치 선택. **WASAPI 루프백** 포함 | Windows 에서 스피커 소리(`source = "system"`)를 보낼 때는 이것만 됩니다 |
| `ffmpeg` | ffmpeg 가 장치를 직접 엶 (dshow / avfoundation / pulse) | soundcard 가 없는 맥·리눅스 |
| `auto` | soundcard 가 있으면 soundcard | 기본 |

`device` 는 비우면 기본 장치입니다. 이름은 `python -m gymhall devices` 로 확인.

---

## Tailscale 노출 방식 — `[stream.tailscale] mode`

| mode | 링크 | 누가 닿나 | 비고 |
|---|---|---|---|
| `serve` (기본) | `https://기기.tailnet.ts.net/live` | tailnet 만 | 서버는 127.0.0.1 에만 열림. HTTPS 인증서 자동 |
| `direct` | `http://기기.tailnet.ts.net:8765/live` | tailnet 만 | 서버를 Tailscale IP 에 직접 엶. 평문 HTTP, 방화벽 허용이 필요할 수 있음. IP 가 바뀌면 서버도 새 IP 로 다시 엶 |
| `funnel` | `https://기기.tailnet.ts.net/live` | **인터넷 전체** | `allow_funnel = true` 필요, `https_port` 는 443/8443/10000 |

**이미 쓰고 있는 serve/funnel 은 건드리지 않습니다.** `https_port` 에 다른
serve 가 걸려 있으면 시작하지 않고 알려 줍니다(`override_existing = true`
로 덮어쓸 수 있음). 그럴 때는 `https_port = 10000` 처럼 다른 포트를 쓰세요.
끝날 때는 자기가 켠 포트만 끕니다(`teardown`).

`link_check_sec` 마다 tailnet 상태를 다시 읽어, 기기 이름·IP 가 바뀌어
**링크가 바뀌면** 새 링크를 다시 올립니다. Tailscale 이 잠깐 끊기면
경고만 남기고 돌아오면 다시 확인합니다.

---

## 링크 → GitHub 이슈 — `[stream.issue]`

| 언제 | 무엇을 | 설정 |
|---|---|---|
| 송출이 시작돼 `min_uptime_sec` 동안 소리가 실제로 나옴 | 새 이슈 | `title`, `body`, `labels`, `assignees`, `mentions` |
| 링크가 바뀜 | 새 이슈 + 옛 이슈 닫기 / 같은 이슈에 댓글 / 아무것도 안 함 | `on_change = "new" \| "comment" \| "none"`, `close_previous` |
| 같은 링크로 열린 이슈가 이미 있음 (토큰 고정 시) | 새로 만들지 않음 | `dedupe` |
| 송출 끝 | 길이·청취 수 댓글, 닫기 | `comment_on_stop`, `close_on_stop` |

안전장치:

- `require_private_repo = true`(기본) — public 저장소에는 올리지 않습니다.
- `include_token = false` 면 링크의 토큰을 `***` 로 가리고 올립니다.
- 장치가 틀려 바로 죽는 송출은 링크를 올리지 않습니다(`min_uptime_sec`).
- 이슈 올리기가 실패해도 송출은 멈추지 않습니다(로그에 남음).
- 라벨이 없으면 `label_color` 로 만듭니다.

제목·본문 템플릿에 쓸 수 있는 자리:

```
{link} {player} {host} {tailnet} {mode} {label} {machine}
{source} {codec} {bitrate} {started} {until} {reason} {token_note}
```

모르는 `{이름}` 은 그대로 남깁니다(오타 하나로 송출이 멈추지 않게).

```toml
[stream.issue]
title    = "[LIVE] {label} ({started})"
body     = """
지금 송출 중입니다: {link}
기기 {host} · {codec} {bitrate}kbps
"""
labels    = ["live-stream", "lecture"]
assignees = ["seongbin45"]
on_change = "comment"
```

---

## 시간표가 최우선

`config.toml` 의 `[[recording.timetable]]` 이 송출의 시작과 끝을 정합니다.

```sh
python -m gymhall stream schedule          # 시간표대로 기다렸다가 수업마다 송출
python -m gymhall stream schedule --show   # 송출할 수업만 보기
```

- 수업 시간 안이면 송출은 **그 수업이 끝나는 시각에 끝납니다.** 그 전에는
  최대 송출 시간, 청취자 없음, 장애 어느 것으로도 멈추지 않습니다.
- `stream start` 를 수업 중에 켜면 그 수업 끝에 맞춰 끝나고, 이름도 수업
  이름이 됩니다. 수업 밖에서 켠 송출이 수업 시간에 들어가면 그 수업을
  넘겨받습니다(이슈에 댓글).
- 수업마다 새 토큰 → 새 링크 → 새 이슈입니다.
- `timetable_slots = ["자료구조"]` 로 송출할 수업만 고르고,
  `pre_roll_minutes` / `post_roll_minutes` 로 앞뒤 여유를 줍니다.
- 작업 스케줄러: `register_tasks.ps1 -Stream` 이면 로그온 시
  `stream_schedule.bat` 이 창 없이 떠서 기다립니다.

## 멈춤 조건 — 기본은 전부 없음

| 설정 | 기본 | 뜻 |
|---|---|---|
| `max_duration_minutes` | `0` (없음) | 닿으면 `on_max_duration` 대로 |
| `on_max_duration` | `"rollback"` | 멈추지 않고 인코더를 새로 띄워 이어 감(아래 롤백). `"stop"` 이면 멈춤 |
| `idle_stop_minutes` (`--idle-stop`) | `0` (없음) | 청취자가 이만큼 없으면 멈춤 |
| `restart_on_failure` | `0` (무제한) | 연속 복구 실패 허용 횟수 |
| `max_listeners` | `0` (무제한) | 동시 청취자 상한 |
| `client_buffer_kb` | `2048` | 이만큼 밀린 청취자 **한 명만** 끊음(송출은 계속) |
| `-m` (명령줄) | — | 직접 준 길이는 '그만큼만' 이라는 뜻이라 멈춤 |

시간표 수업 중에는 `"stop"` 이든 `idle_stop_minutes` 든 무시하고 수업 끝까지 갑니다.

## 롤백 — 멈추지 않고 되돌리기

다음이 생기면 세션을 끝내지 않고 **마지막으로 잘 돌던 상태로 되돌려** 다시 띄웁니다.
토큰·링크·이슈는 그대로라 듣는 사람은 같은 링크로 다시 연결하면 됩니다.

| 무엇이 | 어떻게 되돌리나 |
|---|---|
| 인코더(ffmpeg)가 죽음, 소리가 안 나옴 | 마지막으로 소리가 나온 장치 → 설정한 `device` → 기본 장치(`fallback_default_device`) 순으로 다시 띄움 |
| 최대 송출 시간 도달 (`on_max_duration = "rollback"`) | 새 인코더로 순환. 장시간 송출의 누적 문제를 끊음 |
| `tailscale serve` 설정이 사라짐 | 다시 켬 |
| 오디오 서버 스레드가 멈춤 | 같은 주소로 다시 엶 |
| Tailscale IP 변경 (direct) | 새 IP 로 서버를 다시 엶 + 새 링크 이슈 |
| Tailscale 연결 끊김 | 송출은 계속, 돌아오면 링크 재확인 |
| 시작할 때 Tailscale 미연결 | 송출을 먼저 띄우고, 연결되면 링크를 올림 |

복구가 실패하면 `restart_backoff_sec`(5초)부터 두 배씩
`restart_backoff_max_sec`(120초)까지 늘려 가며 계속 시도합니다.
장치 이름이 틀렸거나 장치가 빠진 경우는 기본 장치로 내려가 이어 갑니다(로그에 경고).
'Windows 에서 ffmpeg 로 스피커 소리' 처럼 **조합 자체가 안 되는 설정**만은 다시
해도 같으니 바로 알려 주고 멈춥니다.

`comment_on_rollback = true` 면 복구할 때마다 이슈에 댓글을 답니다.
끝날 때 댓글에는 복구 횟수가 항상 들어갑니다.

### 실기 확인 (2026-11-16, Windows 11, Tailscale 1.104)

`https_port = 10000`, 시간표 18:38–18:43, `max_duration_minutes = 0.75` 로 실제 이슈를 올렸습니다.

- 18:40:11 이슈 생성, tailnet 주소로 재생 확인
- ffmpeg 를 강제로 죽임 → 5초 뒤 롤백 완료, **같은 링크로 다시 재생**
- 0.75분마다 순환 롤백 2회 (멈추지 않음)
- 18:43:00 시간표 끝에 정확히 종료, serve 10000 끔, 이슈에
  "시간표 종료 · 3분 · 청취 2회 · 롤백 3회" 댓글 후 닫힘

## 접속 토큰

`require_token = true`(기본)면 링크 끝에 `?t=<토큰>` 이 붙고, 토큰이 틀리면
403 입니다. `token` 을 비우면 송출마다 새로 만듭니다(권장) — 지난 이슈의
링크로는 다음 송출을 들을 수 없습니다. 고정하려면 12자 이상을 적으세요.

## 듣는 쪽

- 브라우저: 이슈의 **재생 페이지** 링크
- VLC: 미디어 > 네트워크 스트림 열기 > **듣기** 링크
- 휴대폰: Tailscale 앱으로 tailnet 에 접속한 뒤 브라우저에서 링크

## 안 될 때

| 증상 | 원인 / 해결 |
|---|---|
| `HTTPS 443 포트를 이미 다른 것이 쓰고 있습니다` | 기존 serve/funnel. `https_port = 10000` 처럼 바꾸기 |
| `serve/funnel 은 MagicDNS 이름이 필요합니다` | 관리 콘솔 DNS 탭에서 MagicDNS·HTTPS 켜기, 또는 `mode = "direct"` |
| direct 모드인데 다른 기기에서 안 열림 | Windows 방화벽에서 Python 의 개인/공용 네트워크 허용 |
| 링크는 열리는데 무음 | `source`/`device` 확인 (`python -m gymhall devices`) |
| 강제 종료 뒤 serve 가 남음 | `python -m gymhall stream cleanup` |
| 이슈가 안 올라옴 | 로그 확인: public 저장소, 토큰 권한, 이슈 기능 꺼짐 |
