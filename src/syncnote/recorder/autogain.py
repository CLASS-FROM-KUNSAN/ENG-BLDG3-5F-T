"""마이크 소리 자동 증폭(ffmpeg dynaudnorm).

강의실 PC(HUM-2F-1244, 2026-11-26 수업 중 실측): 마이크 트랙이 RMS -58 ~ -79 dBFS 로, 말소리가
보통 들어오는 -30 ~ -20 dBFS 보다 30 ~ 50 dB 작아 녹화 영상·송출에서 들리지 않았습니다. Windows 입력
레벨은 이미 최대(100% = +12 dB)라 설정으로는 더 키울 수 없었습니다. 그래서 받은 소리를 프로그램이
키웁니다.

고정 배율로 키우면 조용한 구간은 들리게 되지만 큰 소리(박수·마이크 두드림)는 찢어집니다. dynaudnorm 은
짧은 구간마다 크기를 재어 작은 곳은 많이(최대 max_db), 큰 곳은 적게 키우므로 둘 다 피합니다.
녹화(mux)는 마이크·스테레오 믹스 트랙에, 송출은 source = "mic" 일 때 적용합니다.
"""

from __future__ import annotations

# 증폭하는 트랙(시스템 소리는 원래 크기가 맞으므로 건드리지 않음)
GAIN_TRACKS = ("mic", "stereomix")
DEFAULT_MAX_DB = 40.0


def mic_filter(max_db: float = DEFAULT_MAX_DB) -> str:
    """ffmpeg 오디오 필터 문자열. max_db <= 0 이면 "" (끔).

    f=150 ms 구간, g=15 구간 가우스 창(급변을 부드럽게, 지연 약 1초), p=0.9 목표 최대치,
    m = 최대 배율(배수), s=12 압축 조금(갑자기 큰 소리를 눌러 줌)."""
    if max_db <= 0:
        return ""
    factor = min(100.0, 10 ** (float(max_db) / 20))       # dynaudnorm 의 m 은 1~100
    return f"dynaudnorm=f=150:g=15:p=0.9:m={factor:.1f}:s=12"


def from_config(cfg, prefix: str = "recording.audio") -> str:
    """설정에서 필터를 만듭니다. <prefix>.mic_auto_gain(기본 true)·mic_auto_gain_max_db(기본 40)."""
    if not bool(cfg.get(f"{prefix}.mic_auto_gain", True)):
        return ""
    return mic_filter(float(cfg.get(f"{prefix}.mic_auto_gain_max_db", DEFAULT_MAX_DB)))
