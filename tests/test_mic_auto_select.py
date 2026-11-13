"""마이크 자동 선택.

2026-11-13 실기 PC 의 기본 마이크는 '마이크 배열 (Audio Share)' 라는
가상 장치였고, 61초 녹음에 0 아닌 바이트가 **하나도** 없었습니다.
설정을 비워 둔 PC 는 OS 기본 장치를 그대로 쓰므로 수업 내내 비트
단위 0 이 녹음되고, 끝나고서야 압니다.

여기서 하는 일은 좋은 마이크를 고르는 것이 아니라 **죽은 마이크를
거르는 것**까지입니다. 잡음 크기로는 품질을 판단할 수 없습니다.

테스트는 이 코드베이스가 네 번 당한 함정을 각각 겨냥합니다.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from gymhall.paths import Layout
from gymhall.recorder import devices


class _Rec:
    def __init__(self, level):
        self.level = level

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def record(self, numframes):
        time.sleep(0.005)
        if self.level == 0.0:
            # 비트 단위로 정확히 0. 진짜 마이크는 이렇게 안 나옵니다.
            return np.zeros((numframes, 2), dtype=np.float32)
        n = np.arange(numframes) / 48000.0
        wave = self.level * np.sin(2 * np.pi * 440 * n)
        return np.stack([wave] * 2, axis=1).astype(np.float32)


class FakeMic:
    def __init__(self, name, level=0.5, fail=None):
        self.name = name
        self.level = level
        self.fail = fail
        self.opened = 0

    def recorder(self, samplerate, channels, blocksize):
        self.opened += 1
        if self.fail:
            raise OSError(self.fail)
        return _Rec(self.level)


def probe(mic, **kw):
    return devices.probe_mic(mic, duration_sec=0.05, **kw)


# --- probe --------------------------------------------------------------
def test_a_live_mic_is_not_dead():
    r = probe(FakeMic("인텔", level=0.5))
    assert r.peak > 0 and not r.dead


def test_a_bit_exact_zero_mic_is_dead():
    """조용한 방이 아니라 신호 경로가 없는 것입니다."""
    r = probe(FakeMic("Audio Share", level=0.0))
    assert r.peak == 0.0 and r.dead


def test_a_very_quiet_mic_is_alive():
    """잡음이라도 들어오면 장치는 살아 있습니다. 임계값으로 자르면 안 됩니다."""
    r = probe(FakeMic("Focusrite", level=3e-5))
    assert r.peak > 0 and not r.dead


def test_a_mic_that_cannot_be_opened_is_dead():
    r = probe(FakeMic("사용 중", fail="다른 앱이 쓰는 중"))
    assert r.dead and "다른 앱" in (r.error or "")


def test_probing_writes_nothing_to_disk(tmp_path):
    """수업 전 강의실 대화가 파일로 남으면 안 됩니다."""
    before = set(tmp_path.rglob("*"))
    probe(FakeMic("인텔", level=0.5))
    assert set(tmp_path.rglob("*")) == before


# --- 고르기 --------------------------------------------------------------
def select(default, others, **kw):
    return devices.select_working_mic(duration_sec=0.05, default=default,
                                      mics=others, **kw)


def test_a_working_default_is_used_as_is():
    d = FakeMic("인텔", level=0.5)
    other = FakeMic("Focusrite", level=0.5)
    mic, probes = select(d, [other])
    assert mic is d
    assert other.opened == 0, "기본이 멀쩡한데 다른 장치까지 열었습니다"


def test_a_dead_default_is_replaced(caplog):
    """실기에서 난 바로 그 경우입니다."""
    d = FakeMic("마이크 배열 (Audio Share)", level=0.0)
    real = FakeMic("인텔® 스마트 사운드", level=0.5)
    mic, probes = select(d, [real])
    assert mic is real
    assert [p.dead for p in probes] == [True, False]


def test_the_search_stops_at_the_first_live_one():
    d = FakeMic("가상", level=0.0)
    first = FakeMic("인텔", level=0.5)
    later = FakeMic("Focusrite", level=0.5)
    mic, _ = select(d, [first, later])
    assert mic is first
    assert later.opened == 0, "이미 찾았는데 끝까지 다 열었습니다"


def test_all_dead_still_returns_the_default():
    """포기하면 영상만 남습니다. 시도하고 경고하는 쪽이 낫습니다."""
    d = FakeMic("가상1", level=0.0)
    other = FakeMic("가상2", level=0.0)
    mic, probes = select(d, [other])
    assert mic is d
    assert len(probes) == 2 and all(p.dead for p in probes)


def test_the_default_is_not_probed_twice():
    """기본 장치가 목록에도 들어 있습니다. 두 번 열면 3초가 빕니다."""
    d = FakeMic("인텔", level=0.0)
    same = FakeMic("인텔", level=0.0)
    _, probes = select(d, [same])
    assert len(probes) == 1, [p.name for p in probes]


def test_no_mics_at_all_is_not_a_crash():
    mic, probes = select(None, [])
    assert mic is None and probes == []


# --- 세션 배선 (원칙 1: 실물에 묶습니다) ---------------------------------
class Cfg:
    def __init__(self, d=None):
        self._d = d or {}

    def get(self, key, default=None):
        return self._d.get(key, default)


def session(tmp_path, **cfg):
    from gymhall.recorder.session import RecordingSession

    base = {"recording.audio.mic_enabled": True,
            "recording.audio.mic_auto_select": True,
            "recording.audio.mic_probe_sec": 0.05}
    base.update(cfg)
    return RecordingSession(Cfg(base), Layout(tmp_path), "수업")


def test_the_session_asks_for_a_mic(tmp_path, monkeypatch):
    """probe 호출 자체를 지우면 빨개져야 합니다."""
    called = []
    chosen = FakeMic("인텔", level=0.5)
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: (called.append(kw), (chosen, []))[1])
    assert session(tmp_path)._resolve_mic() is chosen
    assert called, "자동 선택을 부르지 않았습니다"


def test_an_explicit_mic_device_is_never_probed(tmp_path, monkeypatch):
    """사람이 고른 것을 코드가 뒤집으면 안 됩니다."""
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: pytest.fail("명시 지정인데 시험했습니다"))
    s = session(tmp_path, **{"recording.audio.mic_device": "Focusrite"})
    assert s._resolve_mic() is None, "리졸버가 이름으로 찾아야 합니다"


def test_auto_select_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: pytest.fail("꺼 뒀는데 시험했습니다"))
    s = session(tmp_path, **{"recording.audio.mic_auto_select": False})
    assert s._resolve_mic() is None


def test_a_disabled_mic_is_not_probed(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: pytest.fail("마이크를 껐는데 시험했습니다"))
    s = session(tmp_path, **{"recording.audio.mic_enabled": False})
    assert s._resolve_mic() is None


def test_the_mic_is_chosen_only_once(tmp_path, monkeypatch):
    """세그먼트마다 다시 고르면 그때마다 소리가 1.5초 빕니다."""
    calls = []
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: (calls.append(1), (FakeMic("인텔"), []))[1])
    s = session(tmp_path)
    s._resolve_mic()
    s._resolve_mic()
    s._resolve_mic()
    assert len(calls) == 1, f"{len(calls)}번 시험했습니다"


def test_choosing_nothing_is_remembered_too(tmp_path, monkeypatch):
    """None('쓸 게 없다')과 _UNSET('아직 안 골랐다')을 섞으면 매번 다시 고릅니다."""
    calls = []
    monkeypatch.setattr(devices, "select_working_mic",
                        lambda **kw: (calls.append(1), (None, []))[1])
    s = session(tmp_path)
    s._resolve_mic()
    s._resolve_mic()
    assert len(calls) == 1


# --- 고른 장치가 실제로 열리는가 (원칙 1 + 6) ----------------------------
def test_the_chosen_mic_is_the_one_actually_opened(tmp_path, monkeypatch):
    """AudioRecorder 가 mic_source 를 무시하고 리졸버를 부르면 빨개집니다."""
    from gymhall.recorder.audio import AudioRecorder

    chosen = FakeMic("인텔", level=0.5)
    monkeypatch.setattr(devices, "resolve_mic_source",
                        lambda name: pytest.fail("고른 장치를 버리고 리졸버를 불렀습니다"))
    rec = AudioRecorder(out_dir=tmp_path, system_enabled=False,
                        mic_enabled=True, mic_source=chosen)
    rec.start("seg")
    time.sleep(0.2)
    rec.stop()
    assert chosen.opened > 0, "고른 장치를 열지 않았습니다"


def test_without_a_chosen_mic_the_resolver_still_runs(tmp_path, monkeypatch):
    from gymhall.recorder import audio as audio_mod

    used = []
    monkeypatch.setattr(audio_mod.devices, "resolve_mic_source",
                        lambda name: used.append(name) or FakeMic("기본", level=0.5))
    rec = audio_mod.AudioRecorder(out_dir=tmp_path, system_enabled=False,
                                  mic_enabled=True, mic_device="Focusrite")
    rec.start("seg")
    time.sleep(0.15)
    rec.stop()
    assert used == ["Focusrite"]


def test_the_system_track_is_never_replaced(tmp_path, monkeypatch):
    """루프백은 재생 중인 게 없으면 정확히 0 이 정상입니다. 오탐의 원천입니다."""
    from gymhall.recorder import audio as audio_mod

    sys_mic = FakeMic("스피커 루프백", level=0.5)
    monkeypatch.setattr(audio_mod.devices, "resolve_system_source",
                        lambda name: sys_mic)
    rec = audio_mod.AudioRecorder(out_dir=tmp_path, system_enabled=True,
                                  mic_enabled=False,
                                  mic_source=FakeMic("엉뚱", level=0.5))
    rec.start("seg")
    time.sleep(0.15)
    rec.stop()
    assert sys_mic.opened > 0, "시스템 트랙에 마이크를 끼워 넣었습니다"


# --- 로그 ----------------------------------------------------------------
def test_all_dead_blames_permissions_not_the_device(tmp_path, caplog):
    """하나만 0 이면 장치 문제, 전부 0 이면 권한 문제입니다."""
    from gymhall.recorder.session import RecordingSession

    probes = [devices.ProbeResult("가상1", 0.0),
              devices.ProbeResult("가상2", 0.0)]
    with caplog.at_level("ERROR", logger="gymhall"):
        RecordingSession._log_mic_choice(FakeMic("가상1"), probes)
    assert "개인 정보" in caplog.text and "마이크에 액세스" in caplog.text


def test_a_replacement_says_what_to_put_in_the_config(tmp_path, caplog):
    from gymhall.recorder.session import RecordingSession

    probes = [devices.ProbeResult("마이크 배열 (Audio Share)", 0.0),
              devices.ProbeResult("인텔® 스마트 사운드", 0.00089)]
    with caplog.at_level("INFO", logger="gymhall"):
        RecordingSession._log_mic_choice(FakeMic("인텔"), probes)
    assert "Audio Share" in caplog.text and "인텔" in caplog.text
    assert "mic_device" in caplog.text, "어디를 고치라는 건지 안 알려 줍니다"


def test_a_normal_default_does_not_nag(caplog):
    from gymhall.recorder.session import RecordingSession

    with caplog.at_level("INFO", logger="gymhall"):
        RecordingSession._log_mic_choice(
            FakeMic("인텔"), [devices.ProbeResult("인텔", 0.5)])
    assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)
