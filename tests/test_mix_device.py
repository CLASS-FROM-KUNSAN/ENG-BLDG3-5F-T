"""'스테레오 믹스' 는 마이크입니다(사용자: "스테레오 믹스가 마이크라니깐").

메인보드 마이크 단자에 꽂은 마이크가 Realtek 드라이버에서 '스테레오 믹스' 로 보입니다(강의실 PC).
예전에는 이 이름을 '스피커 소리를 다시 녹음하는 장치' 로 보고 마이크에서 빼서, 마이크가 이것뿐인
PC 는 마이크 녹음을 하지 않았고 송출(source = "mic")은 '실제 마이크가 없습니다' 로 시작조차 못 하고
재시도만 반복했습니다. 이제 이름으로 거르지 않고 다른 마이크와 똑같이 다룹니다.
"""

import types

import pytest

from syncnote.recorder import devices as D


def _mic(name):
    return types.SimpleNamespace(name=name, id=name)


MIX = _mic("스테레오 믹스(Realtek(R) Audio)")
OTHER = _mic("마이크 배열(디지털 마이크용 인텔® 스마트 사운드 기술)")


class _SC:
    def __init__(self, default, mics):
        self._default, self._mics = default, mics

    def default_microphone(self):
        return self._default

    def all_microphones(self, include_loopback=False):
        return list(self._mics)


def test_stereo_mix_default_is_the_mic(monkeypatch):
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX]))
    assert D.resolve_mic_source("") is MIX
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX, OTHER]))
    assert D.resolve_mic_source("") is MIX, "기본 장치를 이름 때문에 건너뛰면 안 됨"


def test_no_input_device_at_all_is_the_only_no_mic(monkeypatch):
    monkeypatch.setattr(D, "_sc", lambda: _SC(None, []))
    with pytest.raises(D.NoMicrophone):
        D.resolve_mic_source("")


def test_stereo_mix_is_probed_and_chosen_like_any_mic(monkeypatch):
    seen = []

    def probe(mic, **_k):
        seen.append(mic.name)
        return D.ProbeResult(mic.name, 0.02)
    monkeypatch.setattr(D, "probe_mic", probe)
    mic, _p = D.select_working_mic(mics=[MIX], default=MIX)
    assert mic is MIX and seen == [MIX.name]
    seen.clear()
    mic, _p = D.select_working_mic(mics=[OTHER, MIX], default=MIX)
    assert mic is MIX and seen == [MIX.name], "기본 장치(스테레오 믹스)를 먼저 시험"


def test_devices_listing_shows_stereo_mix_as_a_plain_mic(monkeypatch):
    monkeypatch.setattr(D, "list_devices", lambda: [
        D.Device("스피커(Realtek(R) Audio)", "s", "speaker", is_default=True),
        D.Device("스피커(Realtek(R) Audio)", "l", "loopback"),
        D.Device(MIX.name, "m", "microphone", is_default=True)])
    text = D.describe()
    assert f'"{MIX.name}"  <- 기본' in text
    for gone in ("다시 녹음", "자동 선택하지 않음", "실제 마이크가 없습니다", "마이크가 없을 때만"):
        assert gone not in text
