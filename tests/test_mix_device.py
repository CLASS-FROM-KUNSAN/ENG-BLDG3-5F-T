"""'스테레오 믹스' 류는 마이크로 자동 선택하지 않기.

강의실 PC(HUM-3F-1331)에는 진짜 마이크가 없고 녹음 장치가
'스테레오 믹스(Realtek(R) Audio)' 하나였습니다. 그대로 두면 기본 녹음 장치인 이것이
마이크 트랙이 되어, 이미 루프백으로 녹음하는 시스템 소리가 두 번 들어갑니다.
"""

import types

import pytest

from syncnote.recorder import devices as D


def _mic(name):
    return types.SimpleNamespace(name=name, id=name)


MIX = _mic("스테레오 믹스(Realtek(R) Audio)")
REAL = _mic("마이크 배열(디지털 마이크용 인텔® 스마트 사운드 기술)")


@pytest.mark.parametrize("name,mix", [
    ("스테레오 믹스(Realtek(R) Audio)", True), ("Stereo Mix (Realtek High Definition Audio)", True),
    ("What U Hear (Sound Blaster Z)", True), ("Wave Out Mix", True),
    ("마이크 배열(인텔)", False), ("Microphone (Realtek(R) Audio)", False),
    ("Jump Desktop Virtual Microphone", False), ("헤드셋 마이크(USB Audio)", False)])
def test_is_mix_device(name, mix):
    assert D.is_mix_device(name) is mix


def test_only_a_mix_device_means_no_mic_without_probing(monkeypatch):
    monkeypatch.setattr(D, "probe_mic", lambda *a, **k: pytest.fail("믹스는 시험하지 않음"))
    assert D.select_working_mic(mics=[MIX], default=MIX) == (None, [])


def test_default_mix_is_skipped_for_the_real_mic(monkeypatch):
    seen = []

    def probe(mic, **_k):
        seen.append(mic.name)
        return D.ProbeResult(mic.name, 0.01)
    monkeypatch.setattr(D, "probe_mic", probe)
    mic, probes = D.select_working_mic(mics=[MIX, REAL], default=MIX)
    assert mic is REAL and seen == [REAL.name]


def test_dead_real_mic_is_still_tried_when_default_was_a_mix(monkeypatch):
    monkeypatch.setattr(D, "probe_mic", lambda mic, **_k: D.ProbeResult(mic.name, 0.0))
    mic, _p = D.select_working_mic(mics=[MIX, REAL], default=MIX)
    assert mic is REAL                                   # 전부 0 이어도 진짜 마이크로 시도


class _SC:
    def __init__(self, default, mics):
        self._default, self._mics = default, mics

    def default_microphone(self):
        return self._default

    def all_microphones(self, include_loopback=False):
        return list(self._mics)


def test_resolver_never_falls_back_to_a_mix(monkeypatch):
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX]))
    with pytest.raises(D.NoMicrophone, match="스테레오 믹스"):
        D.resolve_mic_source("")
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX, REAL]))
    assert D.resolve_mic_source("") is REAL
    monkeypatch.setattr(D, "_sc", lambda: _SC(REAL, [MIX, REAL]))
    assert D.resolve_mic_source("") is REAL


def test_a_person_may_still_choose_the_mix_by_name(monkeypatch):
    monkeypatch.setattr(D, "_sc", lambda: _SC(REAL, [MIX, REAL]))
    assert D.resolve_mic_source("스테레오") is MIX


def test_devices_listing_marks_mix_and_says_there_is_no_mic(monkeypatch):
    monkeypatch.setattr(D, "list_devices", lambda: [
        D.Device("스피커(Realtek(R) Audio)", "s", "speaker", is_default=True),
        D.Device("스피커(Realtek(R) Audio)", "l", "loopback"),
        D.Device(MIX.name, "m", "microphone", is_default=True)])
    text = D.describe()
    assert "마이크로 자동 선택하지 않음" in text and "실제 마이크가 없습니다" in text
