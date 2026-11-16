#!/bin/sh
# =====================================================================
#  실시간 오디오 송출 (Tailscale 망 안에서만). macOS·Linux 용.
#  링크가 생기면 GitHub 이슈로 올라갑니다. 설정은 config.toml 의 [stream].
#
#    scripts/stream_now.sh                     설정대로
#    scripts/stream_now.sh -l 세미나 -m 90     이름과 길이
#    scripts/stream_now.sh --source mic        마이크로
#
#  Ctrl+C 로 멈추면 tailscale serve 를 끄고 이슈를 닫습니다.
# =====================================================================
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export PYTHONDONTWRITEBYTECODE=1
[ -f "$REPO/config.toml" ] && export GYMHALL_CONFIG="$REPO/config.toml"

if [ -x "$REPO/.venv/bin/python" ]; then
    PY="$REPO/.venv/bin/python"
else
    PY=$(command -v python3 || command -v python)
fi

exec "$PY" -m gymhall stream start "$@"
