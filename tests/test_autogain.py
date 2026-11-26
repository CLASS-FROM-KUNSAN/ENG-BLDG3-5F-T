"""마이크 자동 증폭(recorder/autogain.py).

강의실 PC(HUM-2F-1244, 2026-11-26 수업 중): 마이크 트랙 RMS -58 ~ -79 dBFS — 녹화 영상·송출에서 마이크
음성이 들리지 않음(사용자 보고). Windows 입력 레벨은 이미 최대(100%). 그 수업의 실제 마이크 녹음 60초에
같은 필터를 걸면 평균 -43.2 → -18.2 dB, 최대 -21.2 → -0.9 dB(0 dB 근처 샘플 17개).
"""

from __future__ import annotations

import shutil
import subprocess
import types
from pathlib import Path

import pytest

from syncnote.recorder import autogain as G
from syncnote.recorder import mux as M
from syncnote.stream import capture


def test_filter_values():
    f = G.mic_filter(40)
    assert f.startswith("dynaudnorm=") and "m=100.0" in f
    assert "m=10.0" in G.mic_filter(20)
    assert G.mic_filter(0) == ""
    assert "m=100.0" in G.mic_filter(60)                   # dynaudnorm 의 최대 배율은 100


def test_from_config():
    cfg = types.SimpleNamespace(get=lambda k, d=None: {"recording.audio.mic_auto_gain": False}.get(k, d))
    assert G.from_config(cfg) == ""
    cfg = types.SimpleNamespace(get=lambda k, d=None: d)
    assert G.from_config(cfg) == G.mic_filter(40)          # 기본은 켬


def _capture_cmd(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, timeout=None):
        seen.append(cmd)
        Path(cmd[-1]).write_bytes(b"x")
    monkeypatch.setattr(M, "run", fake_run)
    monkeypatch.setattr(M, "find_ffmpeg", lambda: "ffmpeg")
    wavs = []
    for name in ("system", "mic", "stereomix"):
        p = tmp_path / f"x.{name}.wav"
        p.write_bytes(b"0" * 100)
        wavs.append(M.MuxInput(p, name, 1000.0))
    return seen, wavs


@pytest.mark.parametrize("layout", ["separate", "both", "mixed"])
def test_mux_boosts_only_mic_and_stereomix(monkeypatch, tmp_path, layout):
    seen, wavs = _capture_cmd(monkeypatch, tmp_path)
    M.mux(tmp_path / "o.mka", None, 0.0, wavs, layout=layout, delete_audio=False,
          mic_gain_filter=G.mic_filter(40))
    graph = seen[0][seen[0].index("-filter_complex") + 1]
    assert graph.count("dynaudnorm") == 2                  # mic, stereomix
    assert "[0:a]dynaudnorm" not in graph                   # 시스템 소리(0번)는 그대로
    assert "[1:a]dynaudnorm" in graph and "[2:a]dynaudnorm" in graph


def test_mux_without_filter_is_unchanged(monkeypatch, tmp_path):
    seen, wavs = _capture_cmd(monkeypatch, tmp_path)
    M.mux(tmp_path / "o.mka", None, 0.0, wavs[:2], layout="separate", delete_audio=False)
    assert "-filter_complex" not in seen[0]


def test_stream_adds_the_filter_only_for_the_mic():
    spec = capture.EncodeSpec(audio_filter=G.mic_filter(40))
    cmd = capture.build_ffmpeg(spec, exe="ffmpeg", input_args=None)
    assert cmd[cmd.index("-af") + 1].startswith("dynaudnorm")
    assert "-af" not in capture.build_ffmpeg(capture.EncodeSpec(), exe="ffmpeg", input_args=None)

    from syncnote.stream import live

    s = live.Session.__new__(live.Session)
    s.settings = live.StreamSettings(source="mic")
    assert s._audio_filter().startswith("dynaudnorm")
    s.settings = live.StreamSettings(source="system")
    assert s._audio_filter() == ""
    s.settings = live.StreamSettings(source="mic", mic_auto_gain=False)
    assert s._audio_filter() == ""


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                    reason="ffmpeg/ffprobe 가 없습니다")
def test_a_quiet_mic_becomes_audible_with_real_ffmpeg(tmp_path):
    """-50 dBFS 의 조용한 마이크(수업 실측과 비슷)가 들리는 크기가 됩니다."""
    mic = tmp_path / "x.mic.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:sample_rate=48000,volume=-44dB", "-t", "6", "-ac", "2",
                    str(mic)], check=True)
    out = tmp_path / "o.mka"
    M.mux(out, None, 0.0, [M.MuxInput(mic, "mic", 1000.0)], layout="separate",
          delete_audio=False, mic_gain_filter=G.mic_filter(40))

    def mean_db(path):
        err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                              "volumedetect", "-f", "null", "-"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stderr
        return float(err.split("mean_volume:")[1].split("dB")[0])

    before, after = mean_db(mic), mean_db(out)
    assert before < -45 and after > before + 20, (before, after)
