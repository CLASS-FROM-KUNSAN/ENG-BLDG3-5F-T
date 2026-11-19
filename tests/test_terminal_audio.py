"""터미널 녹음기(FFmpeg / SoX / arecord) 명령 만들기.

명령 빌더는 실행하지 않으므로 어느 OS 에서든 세 플랫폼 경우를 전부
검사할 수 있습니다. 실제 녹음은 실기에서만 확인합니다.
"""

from __future__ import annotations

import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from gymhall import cli
from gymhall.recorder import terminal as t

OUT = Path("out.wav")


def plan(**kw) -> t.Plan:
    return t.Plan(**kw)


# ---------------------------------------------------------------------------
# FFmpeg
# ---------------------------------------------------------------------------
def test_ffmpeg_macos_matches_the_documented_one_liner():
    # ffmpeg -f avfoundation -i ":0" -t 60 output.mp3
    cmd = t.build_ffmpeg(plan(seconds=60, fmt="mp3"), Path("output.mp3"), plat="darwin")
    assert cmd[cmd.index("-f") + 1] == "avfoundation"
    assert cmd[cmd.index("-i") + 1] == ":0"
    assert cmd[cmd.index("-t") + 1] == "60"
    assert "libmp3lame" in cmd and cmd[-1] == "output.mp3"


def test_ffmpeg_macos_device_index_gets_the_audio_colon():
    cmd = t.build_ffmpeg(plan(device="2"), OUT, plat="darwin")
    assert cmd[cmd.index("-i") + 1] == ":2"
    cmd = t.build_ffmpeg(plan(device=":3"), OUT, plat="darwin")
    assert cmd[cmd.index("-i") + 1] == ":3"


def test_ffmpeg_linux_default_and_system_monitor():
    # ffmpeg -f pulse -i default -t 60 output.mp3
    cmd = t.build_ffmpeg(plan(seconds=60), OUT, plat="linux")
    assert cmd[cmd.index("-f") + 1] == "pulse"
    assert cmd[cmd.index("-i") + 1] == "default"
    cmd = t.build_ffmpeg(plan(source="system"), OUT, plat="linux")
    assert cmd[cmd.index("-i") + 1] == t.PULSE_MONITOR


def test_ffmpeg_windows_uses_dshow_with_a_named_device():
    cmd = t.build_ffmpeg(plan(device="마이크 배열"), OUT, plat="win32")
    assert cmd[cmd.index("-f") + 1] == "dshow"
    assert cmd[cmd.index("-i") + 1] == "audio=마이크 배열"


@pytest.mark.parametrize("kw", [{}, {"source": "system", "device": "x"}])
def test_ffmpeg_windows_refuses_what_dshow_cannot_do(kw):
    # 이름 없는 dshow, 그리고 루프백. 둘 다 ffmpeg 가 알아서 실패하는
    # 대신 무엇을 하라고 말해야 합니다.
    with pytest.raises(t.TerminalAudioError):
        t.build_ffmpeg(plan(**kw), OUT, plat="win32")


def test_ffmpeg_macos_system_sound_needs_a_virtual_device():
    with pytest.raises(t.TerminalAudioError, match="BlackHole"):
        t.build_ffmpeg(plan(source="system"), OUT, plat="darwin")


def test_fractional_seconds_survive():
    cmd = t.build_ffmpeg(plan(seconds=1.5), OUT, plat="linux")
    assert cmd[cmd.index("-t") + 1] == "1.5"


# ---------------------------------------------------------------------------
# SoX
# ---------------------------------------------------------------------------
def test_sox_vad_matches_the_documented_one_liner():
    # rec output.wav silence 1 0.1 3% 1 3.0 3%
    cmd = t.build_sox(plan(vad=True, seconds=0), OUT, plat="linux")
    i = cmd.index("silence")
    assert cmd[i:i + 7] == ["silence", "1", "0.1", "3%", "1", "3", "3%"]
    assert "-d" in cmd and "trim" not in cmd


def test_sox_vad_has_a_ceiling_so_it_cannot_run_forever():
    cmd = t.build_sox(plan(vad=True, seconds=600), OUT, plat="linux")
    assert cmd[-3:] == ["trim", "0", "600"]
    # silence 가 trim 앞이어야 '소리 난 뒤부터 600초' 가 됩니다.
    assert cmd.index("silence") < cmd.index("trim")


def test_sox_drivers_per_platform():
    assert t.build_sox(plan(source="system"), OUT, plat="linux")[3:5] == [
        "pulseaudio", t.PULSE_MONITOR]
    assert t.build_sox(plan(device="plughw:1,0"), OUT, plat="linux")[3:5] == [
        "alsa", "plughw:1,0"]
    assert t.build_sox(plan(device="BlackHole"), OUT, plat="darwin")[3:5] == [
        "coreaudio", "BlackHole"]
    assert t.build_sox(plan(device="Mic"), OUT, plat="win32")[3:5] == ["waveaudio", "Mic"]


@pytest.mark.parametrize("kw,plat", [
    ({"fmt": "mp3"}, "linux"),
    ({"source": "system"}, "win32"),
    ({"source": "system"}, "darwin"),
])
def test_sox_refusals(kw, plat):
    with pytest.raises(t.TerminalAudioError):
        t.build_sox(plan(**kw), OUT, plat=plat)


# ---------------------------------------------------------------------------
# arecord
# ---------------------------------------------------------------------------
def test_arecord_matches_the_documented_one_liner():
    # arecord -f cd -d 60 output.wav
    cmd = t.build_arecord(plan(seconds=60), OUT, plat="linux")
    assert cmd[cmd.index("-f") + 1] == "cd"
    assert cmd[cmd.index("-d") + 1] == "60"
    assert cmd[-1] == "out.wav"


def test_arecord_rounds_fractional_seconds_up():
    cmd = t.build_arecord(plan(seconds=1.2), OUT, plat="linux")
    assert cmd[cmd.index("-d") + 1] == "2"


def test_arecord_non_cd_format_is_spelled_out():
    cmd = t.build_arecord(plan(samplerate=48000, channels=1, device="plughw:1,0"),
                          OUT, plat="linux")
    assert "cd" not in cmd
    assert cmd[cmd.index("-r") + 1] == "48000" and cmd[cmd.index("-c") + 1] == "1"
    assert cmd[cmd.index("-D") + 1] == "plughw:1,0"


@pytest.mark.parametrize("kw,plat", [
    ({}, "win32"),
    ({}, "darwin"),
    ({"fmt": "mp3"}, "linux"),
    ({"source": "system"}, "linux"),
])
def test_arecord_refusals(kw, plat):
    with pytest.raises(t.TerminalAudioError):
        t.build_arecord(plan(**kw), OUT, plat=plat)


# ---------------------------------------------------------------------------
# 백엔드 고르기
# ---------------------------------------------------------------------------
ALL = {"ffmpeg": "/x/ffmpeg", "sox": "/x/sox", "arecord": "/x/arecord"}


def test_auto_prefers_ffmpeg_then_sox_then_arecord():
    assert t.choose_backend(plan(), ALL, plat="linux") == "ffmpeg"
    assert t.choose_backend(plan(), {**ALL, "ffmpeg": None}, plat="linux") == "sox"
    only_arecord = {"ffmpeg": None, "sox": None, "arecord": "/x/arecord"}
    assert t.choose_backend(plan(), only_arecord, plat="linux") == "arecord"


def test_auto_never_picks_arecord_off_linux():
    with pytest.raises(t.TerminalAudioError):
        t.choose_backend(plan(), {"ffmpeg": None, "sox": None, "arecord": "/x"}, plat="win32")


def test_vad_goes_to_sox_or_explains_how_to_install_it():
    assert t.choose_backend(plan(vad=True), ALL, plat="linux") == "sox"
    with pytest.raises(t.TerminalAudioError, match="SoX"):
        t.choose_backend(plan(vad=True), {**ALL, "sox": None}, plat="linux")


def test_vad_with_an_explicit_non_sox_backend_is_refused():
    with pytest.raises(t.TerminalAudioError):
        t.build(plan(vad=True, backend="ffmpeg"), OUT, plat="linux", found=ALL)


def test_build_uses_the_found_executable_path():
    backend, cmd = t.build(plan(), OUT, plat="linux", found=ALL)
    assert backend == "ffmpeg" and cmd[0] == "/x/ffmpeg"


# ---------------------------------------------------------------------------
# 파일 이름
# ---------------------------------------------------------------------------
def test_output_name_is_timestamped_like_date_in_the_shell():
    when = datetime(2026, 11, 16, 9, 5, 7)
    assert t.output_name("record_%Y%m%d_%H%M%S", "mp3", when) == "record_20261116_090507.mp3"


def test_output_name_strips_characters_windows_rejects():
    when = datetime(2026, 11, 16, 9, 5, 7)
    assert ":" not in t.output_name("rec_%H:%M", "wav", when)


# ---------------------------------------------------------------------------
# CLI 연결
# ---------------------------------------------------------------------------
def test_cli_parses_audio_record_options():
    args = cli.build_parser().parse_args(
        ["audio", "record", "-b", "sox", "--vad", "-t", "120", "--loop", "--print"])
    assert args.func is cli.cmd_audio_record
    assert (args.backend, args.vad, args.seconds, args.loop, args.print) == (
        "sox", True, 120.0, True, True)


def test_cli_plan_merges_config_and_flags():
    from gymhall import config as cfgmod

    cfg = cfgmod.Config(data=cfgmod._deep_merge(cfgmod.DEFAULTS, {}))
    args = cli.build_parser().parse_args(["audio", "record", "-t", "30", "-f", "flac"])
    p = cli._terminal_plan(cfg, args)
    assert (p.seconds, p.fmt, p.backend, p.vad, p.vad_threshold) == (30.0, "flac", "auto", False, "3%")


def test_cli_print_does_not_record(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(t, "available", lambda: ALL)
    monkeypatch.setattr(t, "record_once", lambda *a, **k: pytest.fail("녹음하면 안 됩니다"))
    monkeypatch.setattr(t.sys, "platform", "linux")
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'[general]\ndata_dir = "{tmp_path.as_posix()}"\n'
                        '[github]\nenabled = false\n[eclass]\nenabled = false\n',
                        encoding="utf-8")
    code = cli.main(["-c", str(cfg_file), "-q", "audio", "record", "--print", "-o", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "# ffmpeg" in out and "pulse" in out


# ---------------------------------------------------------------------------
# Silero VAD + 링 버퍼
# ---------------------------------------------------------------------------
class FakeVADDetector:
    def __init__(self, speech_flags: list[bool], threshold: float = 0.5):
        self.speech_flags = list(speech_flags)
        self.threshold = threshold
        self.available = True
        self.step_calls = 0

    def reset(self):
        pass

    def step(self, chunk):
        self.step_calls += 1
        if self.speech_flags:
            flag = self.speech_flags.pop(0)
            return 0.9 if flag else 0.1
        return 0.1


class FakeChunkRecorder:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes):
        if self.chunks:
            return self.chunks.pop(0)
        return np.zeros((numframes, 2), dtype=np.float32)


class FakeChunkSource:
    def __init__(self, chunks):
        self.chunks = chunks

    def recorder(self, samplerate, channels, blocksize):
        return FakeChunkRecorder(self.chunks)


def test_vad_ring_buffer_preserves_preroll_and_hangover(tmp_path):
    sr = 48000
    ch = 2
    chunk_frames = 1536  # 32ms @ 48kHz

    # 6개 무음 청크 + 4개 발화 청크 + 6개 무음 청크
    silence = np.zeros((chunk_frames, ch), dtype=np.float32)
    speech = np.ones((chunk_frames, ch), dtype=np.float32) * 0.5

    chunks = [silence.copy() for _ in range(6)] + [speech.copy() for _ in range(4)] + [silence.copy() for _ in range(6)]
    flags = [False] * 6 + [True] * 4 + [False] * 6

    vad = FakeVADDetector(flags)
    src = FakeChunkSource(chunks)
    out_file = tmp_path / "test_vad.wav"

    # hangover_sec = 0.096 (3 청크 무음 지속 시 종료)
    # pre_roll_chunks = 15 (충분한 버퍼: 6개 무음이 전부 보존되어야 함)
    res = t.record_vad_ring_buffer(
        out_file,
        plan(samplerate=sr, channels=ch, fmt="wav"),
        vad_detector=vad,
        pcm_source=src,
        hangover_sec=0.096,
        pre_roll_chunks=15,
    )

    assert res is not None
    assert res.exists()

    with wave.open(str(res), "rb") as wf:
        assert wf.getnchannels() == ch
        assert wf.getsampwidth() == 2  # 16비트 PCM
        assert wf.getframerate() == sr
        # 6개 프리롤 + 4개 발화 + 3개 행오버 무음 = 13개 청크
        expected_frames = 13 * chunk_frames
        assert wf.getnframes() == expected_frames


def test_vad_ring_buffer_int16_pcm_integrity(tmp_path):
    sr = 48000
    ch = 2
    chunk_frames = 1536

    # 0.5 진폭의 사인파 청크 2개
    t_axis = np.arange(chunk_frames) / sr
    sine_wave = 0.5 * np.sin(2 * np.pi * 440.0 * t_axis)
    stereo_sine = np.stack([sine_wave] * ch, axis=1).astype(np.float32)

    chunks = [stereo_sine.copy(), stereo_sine.copy()]
    vad = FakeVADDetector([True, True])  # 즉시 발화
    src = FakeChunkSource(chunks)
    out_file = tmp_path / "test_sine.wav"

    res = t.record_vad_ring_buffer(
        out_file,
        plan(samplerate=sr, channels=ch, fmt="wav", seconds=0.064),
        vad_detector=vad,
        pcm_source=src,
        hangover_sec=0.032,
    )

    assert res is not None
    with wave.open(str(res), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        pcm = np.frombuffer(raw, dtype=np.int16)
        # 0.5 * 32767 = 약 16383
        assert np.max(pcm) > 15000
        assert np.min(pcm) < -15000
        # 클리핑이나 NaN/오버플로 없음
        assert np.all(pcm <= 32767)
        assert np.all(pcm >= -32768)


def test_vad_ring_buffer_timeout_when_no_speech(tmp_path):
    chunk_frames = 1536
    silence = np.zeros((chunk_frames, 2), dtype=np.float32)
    chunks = [silence.copy() for _ in range(50)]

    vad = FakeVADDetector([False] * 50)
    src = FakeChunkSource(chunks)
    out_file = tmp_path / "timeout.wav"

    res = t.record_vad_ring_buffer(
        out_file,
        plan(samplerate=48000, channels=2, fmt="wav"),
        vad_detector=vad,
        pcm_source=src,
        max_wait_sec=0.05,
    )

    assert res is None
    assert not out_file.exists()


def test_record_once_falls_back_when_vad_unavailable(tmp_path, monkeypatch):
    recorded_with_cmd = False

    def mock_run(cmd, **kw):
        nonlocal recorded_with_cmd
        recorded_with_cmd = True
        # 가짜 출력 파일 생성 (cmd 에 포함된 .wav 파일 경로)
        out = next(Path(x) for x in cmd if str(x).endswith(".wav"))
        out.write_bytes(b"RIFF" + b"\x00" * 40)
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(t.subprocess, "run", mock_run)
    monkeypatch.setattr(t, "available", lambda: {"sox": "/x/sox", "ffmpeg": "/x/ffmpeg"})

    # VAD 활성화 상태이지만 Silero VAD 는 사용 불가능한 상태
    res = t.record_once(
        plan(vad=True, backend="auto"),
        tmp_path,
        vad_detector=type("UnavailableVAD", (), {"available": False})(),
    )

    assert res is not None
    assert recorded_with_cmd, "Silero VAD 가 없을 때 SoX CLI 백엔드로 정상 폴백되어야 합니다"

