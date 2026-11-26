"""소리 입출력 단계별 점검(recorder/audio_check.py, scripts/audio_check.bat).

사용자 요청: 강의실 PC 에서 직접 했던 시험(무음·톤·잡음 녹음, 증폭)을 사용자가 단계별로 해 볼 수 있게.
가짜 장치로 강의실 PC 의 경우들을 흉내 냅니다(실제 사운드카드 없이).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from syncnote.recorder import audio_check as AC

SR = AC.SR
MIC, MIX, LB = "마이크(Realtek(R) Audio)", "스테레오 믹스(Realtek(R) Audio)", "[루프백] 스피커(Realtek(R) Audio)"


class FakeIO:
    """phase 에 따라 신호를 만듭니다. mic_alive: 말하면 마이크가 커짐. air_ms: 공기 경로 지연(None = 없음)."""

    def __init__(self, *, mic_alive=True, air_ms=3.0, inputs=(MIC, MIX), default=MIC):
        self.mic_alive, self.air_ms, self.inputs, self.default = mic_alive, air_ms, inputs, default
        self.calls, self.played = 0, []
        self.rng = np.random.default_rng(0)

    def describe(self):
        return {"default_mic": self.default, "default_speaker": "스피커(Realtek(R) Audio)",
                "inputs": list(self.inputs)}

    def play(self, x):
        self.played.append(len(x))

    def record(self, seconds, play=None):
        self.calls += 1
        n = int(SR * seconds)
        floor = self.rng.standard_normal(n) * 10 ** (-80 / 20)
        speaking = self.calls == 2            # 2단계(말하기)
        out = {}
        pb = np.zeros(n)
        if play is not None:
            p = np.asarray(play)[:, 0]
            start = int(0.6 * SR)
            pb[start:start + len(p)] = p[: n - start]
        for name in self.inputs:
            x = floor.copy()
            if name == MIC and speaking and self.mic_alive:
                x += 0.05 * np.sin(2 * np.pi * 220 * np.arange(n) / SR)
            if name == MIX:
                x += pb                                       # 재생 소리의 디지털 사본
                if self.air_ms is not None:
                    d = int(self.air_ms * SR / 1000)
                    x[d:] += 0.3 * pb[:-d]                    # 공기를 거쳐 마이크로
            out[name] = np.stack([x, x], axis=1)
        out[LB] = np.stack([pb, pb], axis=1)
        return out


def run(tmp_path, io, answers=(), **kw):
    it = iter(answers)
    said = []
    rep = AC.Check(io, tmp_path / "out", ask=lambda _p: next(it, ""), say=said.append, **kw).run()
    return rep, "\n".join(said)


@pytest.fixture(autouse=True)
def _no_board(monkeypatch):
    from syncnote.recorder import driver_help
    monkeypatch.setattr(driver_help, "baseboard", lambda: ("Gigabyte Technology Co., Ltd.", "H410M HD3P"))


def test_full_run_finds_the_live_mic_and_the_air_path(tmp_path):
    io = FakeIO()
    rep, text = run(tmp_path, io, answers=["", "", "", "y", "", ""])
    assert "마이크 소리가 들어오는 장치: " + MIC in rep["verdicts"]
    assert rep["steps"]["2"]["rise_db"][MIC] > 20
    assert rep["steps"]["3"]["heard"] is True
    assert any("스피커로 나가는 소리도 녹음" in v and MIX in v for v in rep["verdicts"])
    assert rep["steps"]["5"][MIX]["acoustic_delay_ms"] == pytest.approx(3.0, abs=0.3)
    out = tmp_path / "out"
    assert (out / "report.txt").exists() and json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert list(out.glob("2_speech_*_plus30dB.wav")) and list(out.glob("3_listen_*_normalized.wav"))


def test_dead_mic_is_reported(tmp_path):
    rep, _ = run(tmp_path, FakeIO(mic_alive=False), answers=[""] * 6, no_sound=True)
    assert any("들어오지 않거나 아주 작습니다" in v for v in rep["verdicts"])


def test_no_air_copy_is_not_called_a_dead_mic(tmp_path):
    rep, text = run(tmp_path, FakeIO(air_ms=None), answers=[""] * 6)
    v = next(x for x in rep["verdicts"] if MIX in x and "디지털 사본만" in x)
    assert "2단계 결과를 믿으세요" in v


def test_stereo_mix_only_shows_the_driver_guidance(tmp_path):
    rep, text = run(tmp_path, FakeIO(inputs=(MIX,), default=MIX), steps=(0, 6))
    assert "realtek.com/Download" in text and "Gigabyte H410M HD3P" in text
    assert any("드라이버" in v for v in rep["verdicts"])


def test_skip_and_quit(tmp_path):
    io = FakeIO()
    rep, _ = run(tmp_path, io, answers=["s", "q"])
    assert io.calls == 0                                  # 1단계 건너뛰고 2단계에서 끝냄
    assert (tmp_path / "out" / "report.txt").exists()     # 그래도 요약은 남김


def test_auto_mode_never_asks_and_skips_listening(tmp_path):
    io = FakeIO()
    rep = AC.Check(io, tmp_path / "out", ask=lambda _p: pytest.fail("물음"), say=lambda *_: None,
                   auto=True).run()
    assert rep["steps"]["3"]["skipped"] and io.calls == 4      # 1·2·4·5 녹음


def test_no_sound_plays_nothing(tmp_path):
    io = FakeIO()
    run(tmp_path, io, answers=[""] * 6, no_sound=True)
    assert io.played == [] and io.calls == 2


def test_analysis_helpers():
    t = np.arange(SR) / SR
    assert AC.tone_level(0.1 * np.sin(2 * np.pi * 1000 * t)) > 0.05
    assert AC.tone_level(0.1 * np.sin(2 * np.pi * 300 * t)) < 0.001
    lv = AC.levels(np.zeros(100))
    assert lv["nonzero_pct"] == 0.0
    loud, gain = AC.normalize(np.full(1000, 10 ** (-60 / 20)))
    assert gain == 57.0 and abs(20 * np.log10(np.abs(loud).max()) + 3) < 0.1
    assert AC.normalize(np.full(10, 1e-9))[1] == 60.0             # 최대 +60 dB
    assert AC.acoustic_copy([(2.0, 0.9), (2.4, 0.2), (5.0, 0.1)]) == (3.0, 0.1)
    assert AC.acoustic_copy([(2.0, 0.9), (30.0, 0.01)]) is None


def test_cli_and_bat_are_wired():
    from pathlib import Path

    from syncnote import cli

    args = cli.build_parser().parse_args(["audio-check", "--yes", "--no-sound", "--steps", "0,1,6"])
    assert args.func is cli.cmd_audio_check and args.yes and args.no_sound
    bat = (Path(__file__).resolve().parents[1] / "scripts" / "audio_check.bat").read_bytes()
    assert b'-m syncnote audio-check %*' in bat and bat.isascii()


def test_an_exactly_zero_input_is_called_out(tmp_path):
    class ZeroIO(FakeIO):
        def record(self, seconds, play=None):
            out = super().record(seconds, play)
            out["마이크 배열(Audio Share)"] = np.zeros((int(SR * seconds), 2))
            return out
    rep, _ = run(tmp_path, ZeroIO(), steps=(1, 6), answers=[""])
    assert any("Audio Share" in v and "정확히 0" in v for v in rep["verdicts"])
