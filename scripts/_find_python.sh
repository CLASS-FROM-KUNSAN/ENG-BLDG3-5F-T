# shellcheck shell=sh
# =====================================================================
#  파이썬 찾기 (macOS·Linux). 모든 .sh 가 파이썬을 여기서만 고릅니다 — python / python3 / py 를
#  직접 부르지 않습니다. `. "$(dirname "$0")/_find_python.sh"` 로 불러 쓰고, REPO 가 정해져
#  있어야 합니다. 규칙: docs/SETUP.md '파이썬 실행 규칙' (tests/test_scripts.py 가 강제).
#
#  기기마다 명령이 다릅니다. '있는지' 가 아니라 '실제로 실행되고 3.10 이상인지' 로 고르고,
#  하나가 안 되면 다음 방법으로 갑니다:
#    SYNCNOTE_PYTHON(사용자 지정) -> python3 -> python -> py
#  결과: PY = .venv 의 파이썬이 실행되면 그것, 아니면 찾은 시스템 파이썬(전체 경로).
#        못 찾으면 안내를 출력하고 127 로 끝냅니다.
# =====================================================================
_sn_check() {
    "$@" -c 'import sys;(min(sys.version_info[:2],(3,10))==(3,10)) and print(sys.executable)' 2>/dev/null
}

PY=""
if [ -x "$REPO/.venv/bin/python" ]; then
    PY=$(_sn_check "$REPO/.venv/bin/python")
fi
if [ -z "$PY" ]; then
    for _sn_cand in "${SYNCNOTE_PYTHON:-}" python3 python py; do
        [ -n "$_sn_cand" ] || continue
        command -v "$_sn_cand" >/dev/null 2>&1 || [ -x "$_sn_cand" ] || continue
        PY=$(_sn_check "$_sn_cand")
        [ -n "$PY" ] && break
    done
fi
if [ -z "$PY" ]; then
    echo "[X] 파이썬을 실행하지 못했습니다(SYNCNOTE_PYTHON, python3, python, py 를 시도함)." >&2
    echo "    3.10 이상을 설치하거나, 이미 있는 파이썬 경로를 SYNCNOTE_PYTHON 에 적으세요." >&2
    exit 127
fi
