#!/bin/sh
# =====================================================================
#  소리만 녹음합니다 (터미널 녹음기: FFmpeg / SoX / arecord). macOS·Linux 용.
#  설정은 config.toml 의 [recording.terminal] 을 따릅니다.
#
#    scripts/record_audio.sh                    설정대로 한 번
#    scripts/record_audio.sh -t 3000 -f mp3     50분, mp3
#    scripts/record_audio.sh --vad --loop       소리가 날 때마다 파일 하나씩 (SoX)
#    scripts/record_audio.sh -b arecord -t 60   리눅스 기본 ALSA 도구로
#
#  cron 에 걸기 (crontab -e) — 평일 09:00 에 50분:
#    0 9 * * 1-5  /path/to/repo/scripts/record_audio.sh -t 3000 >/dev/null 2>&1
#
#  cron 은 PATH 가 짧습니다(/usr/bin:/bin). brew 로 깐 ffmpeg/sox 가 안 보이면
#  crontab 맨 위에  PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin  을 넣으세요.
#  Linux 에서 PulseAudio/PipeWire 를 쓰려면 cron 에도 사용자 세션 정보가
#  필요해 아래에서 XDG_RUNTIME_DIR 를 채워 둡니다.
#  파일 이름에 날짜와 시각이 들어가므로 매일 돌려도 덮어쓰지 않습니다.
#  창이 없으니 오류는 <데이터 폴더>/logs/ 에서 보세요.
# =====================================================================
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONDONTWRITEBYTECODE=1
[ -f "$REPO/config.toml" ] && export GYMHALL_CONFIG="$REPO/config.toml"
: "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
export XDG_RUNTIME_DIR

if [ -x "$REPO/.venv/bin/python" ]; then
    PY="$REPO/.venv/bin/python"
else
    PY=$(command -v python3 || command -v python)
fi

exec "$PY" -m gymhall audio record "$@"
