"""soundcard 가 형식 때문에 못 여는 마이크(강의실 PC 메인보드 단자)를 ffmpeg DirectShow 로."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from syncnote.recorder import devices
from syncnote.recorder import dshow_mic as D

# soundcard 안에서 터진 것처럼 보이는 AssertionError (mediafoundation.py:516 과 같은 모양)
_SRC = compile("def boom():\n    assert 0x0003 == 0xFFFE\n",
               "site-packages/soundcard/mediafoundation.py", "exec")
_NS: dict = {}
exec(_SRC, _NS)


def _format_error() -> AssertionError:
    try:
        _NS["boom"]()
    except AssertionError as exc:
        return exc
    raise RuntimeError("unreachable")


class SoundcardMic:
    """soundcard 마이크 흉내: recorder() 를 열면 형식 단언으로 죽음."""

    isloopback = False

    def __init__(self, name="마이크(High Definition Audio Device)", error=None):
        self.name = name
        self.error = error or _format_error()

    def recorder(self, **_kw):
        mic = self

        class R:
            def __enter__(self):
                raise mic.error

            def __exit__(self, *a):
                return False
        return R()


class FakeProc:
    def __init__(self, data: bytes, err: bytes = b""):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(err)
        self.terminated = False

    def poll(self):
        return 1

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _pcm(frames: int, ch: int = 2, value: float = 0.25) -> bytes:
    return struct.pack(f"<{frames * ch}f", *([value] * frames * ch))


class _Endless:
    """0.25 로 된 float32 가 끝없이 나오는 stdout."""

    def read(self, n: int) -> bytes:
        return struct.pack("<f", 0.25) * (n // 4)


class EndlessProc(FakeProc):
    def __init__(self):
        super().__init__(b"")
        self.stdout = _Endless()


def test_format_error_is_recognised_only_from_soundcard():
    assert D.is_format_error(_format_error())
    assert not D.is_format_error(AssertionError("우리 코드"))
    assert not D.is_format_error(OSError("장치 없음"))


@pytest.mark.parametrize("want, names, got", [
    ("마이크(High Definition Audio Device)", ("마이크(High Definition Audio Device)",),
     "마이크(High Definition Audio Device)"),
    ("마이크(high definition audio device)", ("마이크(High Definition Audio Device)",),
     "마이크(High Definition Audio Device)"),
    ("마이크(High Definition Audio Device)", ("마이크(High Definition Audio Devic",),
     "마이크(High Definition Audio Devic"),            # DirectShow 가 이름을 자른 경우
    ("마이크(USB)", ("라인 입력(HD)",), None),
    ("", ("마이크(HD)",), None),                       # 빈 이름이 아무 장치나 고르지 않게
])
def test_match_name(want, names, got):
    assert D.match_name(want, names) == got


def test_alternative_only_for_windows_recording_devices():
    mic = SoundcardMic()
    devs = lambda _ff: ("마이크(High Definition Audio Device)",)   # noqa: E731
    alt = D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="win32")
    assert isinstance(alt, D.DshowMic) and alt.name == mic.name
    # 리눅스는 ffmpeg PulseAudio 로(사양 변경: 녹음 방법 사슬에 리눅스도 넣음), macOS 는 없음
    assert D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="linux") is None  # id 없음
    mic.id = "alsa_input.pci-0000_00_1f.3.analog-stereo"
    lin = D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="linux")
    assert isinstance(lin, D.DshowMic) and "pulse" in lin._build(48000, 2)
    assert mic.id in lin._build(48000, 2) and lin.label == "ffmpeg (pulse)"
    assert D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="darwin") is None
    loop = SoundcardMic()
    loop.isloopback = True
    assert D.alternative(loop, ffmpeg="ffmpeg", devices=devs, platform="win32") is None


def test_dshow_recorder_reads_float32_frames_and_stops_ffmpeg():
    procs = []

    def popen(cmd, **_kw):
        procs.append(cmd)
        p = FakeProc(_pcm(100))
        procs.append(p)
        return p
    mic = D.DshowMic("마이크(HD)", "ffmpeg", popen=popen)
    with mic.recorder(samplerate=48000, channels=2) as rec:
        data = rec.record(numframes=60)
        assert data.shape == (60, 2) and data.dtype == np.float32
        assert float(np.max(data)) == pytest.approx(0.25)
        assert rec.record(numframes=60).shape == (40, 2)       # 남은 만큼
    cmd, proc = procs
    assert cmd[cmd.index("-f") + 1] == "dshow" and "audio=마이크(HD)" in cmd
    assert cmd[cmd.index("-ar") + 1] == "48000" and cmd[cmd.index("-ac") + 1] == "2"
    assert proc.terminated


def test_dshow_recorder_reports_why_ffmpeg_stopped():
    """프로세스가 떴다고 열린 게 아님 — 첫 소리 없이 끝나면 '못 엶' 과 그 이유(사양 변경)."""
    mic = D.DshowMic("마이크(HD)", "ffmpeg",
                     popen=lambda *a, **k: FakeProc(b"", b"Could not run graph" + bytes([10])))
    with pytest.raises(RuntimeError, match="Could not run graph"), mic.recorder(channels=2):
        pass


def test_pipe_with_no_sound_times_out_as_open_failure():
    class Never:
        def read(self, n):
            import time
            time.sleep(3)
            return b""

    class Hang(FakeProc):
        def __init__(self):
            super().__init__(b"")
            self.stdout = Never()
    mic = D.DshowMic("마이크(HD)", "ffmpeg", popen=lambda *a, **k: Hang())
    mic.start_timeout = 0.2
    with pytest.raises(RuntimeError, match="소리가 오지 않았습니다"), mic.recorder(channels=2):
        pass


def _alt_factory(seen):
    def alt(source):
        # 진짜 ffmpeg 는 끊임없이 흘려 보냄 — 끝나지 않는 흐름으로 흉내
        mic = D.DshowMic(source.name, "ffmpeg", popen=lambda *a, **k: EndlessProc())
        seen.append(mic)
        return mic
    return alt


def test_open_recording_falls_back_on_format_error():
    seen, why = [], []
    with D.open_recording(SoundcardMic(), samplerate=48000, channels=2,
                          on_fallback=lambda m, w: why.append(w), alt=_alt_factory(seen)) as rec:
        assert rec.record(numframes=10).shape == (10, 2)
    assert len(seen) == 1 and "EXTENSIBLE" in why[0]


def test_open_recording_falls_back_on_any_open_error():
    """사양 변경(사용자 요청 'FFmpeg 가 실패하면?'): 형식 오류만이 아니라 어떤 이유로든
    지금 방법이 안 열리면 사슬의 다음 방법으로 갑니다. 전부 안 되면 방법마다 이유를 적음."""
    seen = []
    with D.open_recording(SoundcardMic(error=OSError("장치를 다른 앱이 쓰는 중")),
                          samplerate=48000, channels=2, alt=_alt_factory(seen)) as rec:
        rec.record(numframes=4)
    assert len(seen) == 1
    with pytest.raises(RuntimeError, match="다른 앱이 쓰는 중"), D.open_recording(
            SoundcardMic(error=OSError("다른 앱이 쓰는 중")), samplerate=48000, channels=2,
            alt=lambda s: None):
        pass


def test_open_recording_without_alternative_explains_instead_of_empty_assert():
    with pytest.raises(RuntimeError, match="형식") as ei, D.open_recording(
            SoundcardMic(), samplerate=48000, channels=2, alt=lambda s: None):
        pass
    assert str(ei.value)                                      # 빈 글자가 아님


def test_prefer_dshow_skips_soundcard():
    seen, why = [], []

    class Never(SoundcardMic):
        def recorder(self, **_kw):
            raise AssertionError("soundcard 를 열면 안 됨")
    with D.open_recording(Never(), samplerate=48000, channels=2, prefer_dshow=True,
                          on_fallback=lambda m, w: why.append(w), alt=_alt_factory(seen)) as rec:
        rec.record(numframes=5)
    assert why == ["설정(backend = dshow)"]


def test_probe_mic_measures_through_the_fallback(monkeypatch):
    monkeypatch.setattr(D, "alternative", _alt_factory([]))
    r = devices.probe_mic(SoundcardMic(), duration_sec=0.01)
    assert r.error is None and r.peak == pytest.approx(0.25) and r.via == "ffmpeg DirectShow"


def test_probe_mic_error_is_never_empty(monkeypatch):
    monkeypatch.setattr(D, "alternative", lambda *a, **k: None)
    r = devices.probe_mic(SoundcardMic(), duration_sec=0.01)
    assert r.dead and r.error and "형식" in r.error


def test_faint_signal_is_flagged():
    assert devices.ProbeResult("m", 0.00037).faint                  # 비어 있는 단자 실측
    assert not devices.ProbeResult("m", 0.02).faint
    assert not devices.ProbeResult("m", 0.0).faint                  # 0 은 '죽음' 쪽


# -- 녹음 방법 사슬 · 녹음 중 복구(정규화 롤백) -------------------------------------
class Flaky:
    """n 번 읽고 나면 끊기는 장치(뽑힘·프로세스 종료 흉내)."""

    isloopback = False

    def __init__(self, name, reads=2, open_error=None, value=0.5):
        self.name, self.reads, self.open_error, self.value = name, reads, open_error, value
        self.opened = 0

    def recorder(self, **_kw):
        dev = self

        class R:
            def __enter__(self):
                dev.opened += 1
                if dev.open_error:
                    raise dev.open_error
                self.left = dev.reads
                return self

            def record(self, numframes=4):
                if self.left <= 0:
                    raise RuntimeError(f"{dev.name} 끊김")
                self.left -= 1
                return np.full((numframes, 2), dev.value, dtype="float32")

            def __exit__(self, *a):
                return False
        return R()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _rr(devs, clock):
    cands = [D._Candidate(d.name, d) for d in devs]
    return D.ResilientRecorder(cands, samplerate=10, channels=2, clock=clock,
                               sleep=clock.sleep).begin()


def test_failover_switches_method_and_fills_the_gap_with_silence():
    clock = Clock()
    a, b = Flaky("soundcard", reads=1), Flaky("ffmpeg", reads=99, value=0.25)
    rr = _rr([a, b], clock)
    assert rr.record(4)[0, 0] == 0.5                       # soundcard
    clock.t = 3.0                                           # 마지막 소리 0초 → 3초에 끊김 발견
    out = rr.record(4)                                      # 끊김 → ffmpeg 로. 첫 소리(4프레임=0.4초)는
    assert out[0, 0] == 0.0                                 # 2.6~3.0초의 것 — 그 앞 2.6초를 무음으로
    frames = len(out)
    while rr.gap_frames:
        frames += len(rr.record(4))
    assert frames == 26 and rr.filled_sec == pytest.approx(2.6)
    assert rr.record(4)[0, 0] == 0.25                       # 이제 ffmpeg 소리
    assert any("soundcard 끊김" in e for e in rr.events) and any("ffmpeg 로 엶" in e for e in rr.events)


def test_all_methods_down_keeps_time_with_silence_and_retries_with_backoff():
    clock = Clock()
    a = Flaky("soundcard", reads=1)
    b = Flaky("ffmpeg", open_error=OSError("ffmpeg 고장"))
    rr = _rr([a, b], clock)
    rr.record(4)
    a.open_error = OSError("장치 뽑힘")                     # 이제 둘 다 안 열림
    silent = rr.record(10)                                  # 실제 시간만큼 무음(1초) — 시간축 유지
    assert silent.shape == (10, 2) and not silent.any() and clock.t == pytest.approx(1.0)
    assert rr.next_try == pytest.approx(0.0 + 2.0)          # 첫 재시도 2초 뒤
    assert any("모든 방법 실패" in e for e in rr.events) and "ffmpeg 고장" in " ".join(rr.errors)
    rr.record(10)
    rr.record(10)                                           # 2초 지남 → 다시 시도(또 실패) → 5초
    assert rr.next_try == pytest.approx(clock.t - 1.0 + 5.0)
    a.open_error = None                                     # 다시 꽂음
    a.reads = 99
    clock.t = rr.next_try
    rr.record(4)
    while rr.gap_frames:
        rr.record(4)                                        # 끊긴 동안의 무음
    assert rr.record(4)[0, 0] == 0.5 and rr.fails == 0


def test_alternatives_order_and_tools():
    src = SoundcardMic()
    tools = {"sox": "sox.exe", "arecord": "arecord"}
    ff = lambda s: D.DshowMic(s.name, "ffmpeg.exe")          # noqa: E731
    win = D.alternatives(src, platform="win32", tool=tools.get, ffmpeg_alt=ff)
    assert [type(m).__name__ for m in win] == ["DshowMic", "SoxMic"]       # arecord 는 리눅스만
    sox_cmd = win[1]._build(48000, 2)
    assert sox_cmd[:5] == ["sox.exe", "-q", "-t", "waveaudio", src.name[:D.WINMM_NAME_MAX]]
    assert "floating-point" in sox_cmd and sox_cmd[-1] == "-"
    lin = D.alternatives(src, platform="linux", tool=tools.get, ffmpeg_alt=ff)
    assert [type(m).__name__ for m in lin] == ["DshowMic", "SoxMic", "ArecordMic"]
    ar = lin[2]._build(16000, 1)
    assert ar[ar.index("-f") + 1] == "FLOAT_LE" and ar[-1] == "-"
    loop = SoundcardMic()
    loop.isloopback = True
    assert D.alternatives(loop, platform="win32", tool=tools.get, ffmpeg_alt=ff) == []


def test_prefer_dshow_puts_ffmpeg_first_but_keeps_soundcard_as_backup():
    ff = D.DshowMic("m", "ffmpeg.exe")
    cands = D._candidates(SoundcardMic(), prefer_dshow=True, alt=None,
                          chain=lambda s: [ff, D.SoxMic("m", "sox")])
    assert [c.label for c in cands] == ["ffmpeg DirectShow", "soundcard", "SoX"]
