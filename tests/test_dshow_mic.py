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
])
def test_match_name(want, names, got):
    assert D.match_name(want, names) == got


def test_alternative_only_for_windows_recording_devices():
    mic = SoundcardMic()
    devs = lambda _ff: ("마이크(High Definition Audio Device)",)   # noqa: E731
    alt = D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="win32")
    assert isinstance(alt, D.DshowMic) and alt.name == mic.name
    assert D.alternative(mic, ffmpeg="ffmpeg", devices=devs, platform="linux") is None
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
    mic = D.DshowMic("마이크(HD)", "ffmpeg",
                     popen=lambda *a, **k: FakeProc(b"", b"Could not run graph\n"))
    with mic.recorder(channels=2) as rec:
        import time
        time.sleep(0.05)                                        # stderr 읽는 스레드
        with pytest.raises(RuntimeError, match="Could not run graph"):
            rec.record(numframes=10)


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


def test_open_recording_keeps_other_errors():
    with pytest.raises(OSError), D.open_recording(
            SoundcardMic(error=OSError("장치가 없습니다")), samplerate=48000, channels=2,
            alt=_alt_factory([])):
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
