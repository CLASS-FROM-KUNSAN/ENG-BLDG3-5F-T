"""스테레오 믹스: 기본 마이크로는 고르지 않고, 따로 한 트랙으로 함께 받아 마이크와 겹쳐 냄.
기본 녹음 장치가 스테레오 믹스이고 마이크 입력이 없으면 Realtek 드라이버를 직접 설치·업데이트
하라고 안내(사용자 요청 — 가장 중요).

메인보드 마이크 단자(3.5mm)에 꽂은 마이크가 Realtek 드라이버에서 '스테레오 믹스' 로만 보이는
PC 가 있습니다(강의실 PC). 예전에는 스테레오 믹스를 아예 녹음하지 않았고, 송출(source = "mic")은
'실제 마이크가 없습니다' 로 시작하지 못한 채 재시도만 반복했습니다.
"""

from __future__ import annotations

import shutil
import subprocess
import types

import pytest

from syncnote.recorder import devices as D


def _mic(name):
    return types.SimpleNamespace(name=name, id=name)


MIX = _mic("스테레오 믹스(Realtek(R) Audio)")
REAL = _mic("마이크(Realtek(R) Audio)")


@pytest.fixture(autouse=True)
def _real_find_mix_source(monkeypatch):
    # conftest 가 실제 장치를 열지 않게 막아 둔 것을 되돌림(여기서는 _sc 를 가짜로 바꿈)
    monkeypatch.setattr(D, "find_mix_source", D._real_find_mix_source)


class _SC:
    def __init__(self, default, mics):
        self._default, self._mics = default, mics

    def default_microphone(self):
        return self._default

    def all_microphones(self, include_loopback=False):
        return list(self._mics)


# --- 장치 ---------------------------------------------------------------------

def test_mix_is_found_for_its_own_track_but_not_as_the_default_mic(monkeypatch):
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX, REAL]))
    assert D.resolve_mic_source("") is REAL           # 기본 마이크 선택에서는 뺌
    assert D.find_mix_source("") is MIX               # 따로 한 트랙으로
    assert D.find_mix_source("", exclude=MIX.name) is None   # 이미 마이크로 쓰면 두 번 열지 않음
    monkeypatch.setattr(D, "_sc", lambda: _SC(REAL, [REAL]))
    assert D.find_mix_source("") is None


def test_find_mix_source_never_raises(monkeypatch):
    def boom():
        raise D.SoundcardMissing()
    monkeypatch.setattr(D, "_sc", boom)
    assert D.find_mix_source("") is None
    assert D.check_mix_only() is None


@pytest.mark.parametrize("default,names,probes,want", [
    (MIX.name, [MIX.name], None, True),                       # 스테레오 믹스뿐
    (MIX.name, [MIX.name, REAL.name], None, False),           # 진짜 마이크가 있음
    (MIX.name, [MIX.name, REAL.name], [D.ProbeResult(REAL.name, 0.0)], True),   # 있지만 죽음
    (MIX.name, [MIX.name, REAL.name], [D.ProbeResult(REAL.name, 0.02)], False),
    (REAL.name, [MIX.name, REAL.name], None, False),          # 기본이 진짜 마이크
])
def test_mix_only_situation(default, names, probes, want):
    assert D.mix_only(default, names, probes) is want


def test_driver_guidance_tells_the_person_what_to_do():
    text = D.driver_guidance(MIX.name)
    assert "https://www.realtek.com/Download/List?cate_id=593" in text
    assert "장치 관리자" in text and "드라이버 업데이트" in text
    assert "mmsys.cpl" in text and "Realtek Audio Console" in text and "4극" in text


def test_devices_listing_shows_the_guidance(monkeypatch):
    monkeypatch.setattr(D, "list_devices", lambda: [
        D.Device("스피커(Realtek(R) Audio)", "l", "loopback"),
        D.Device(MIX.name, "m", "microphone", is_default=True)])
    text = D.describe()
    assert "따로 한 트랙으로 함께 녹음" in text
    assert D.REALTEK_DRIVER_URL in text


def test_no_guidance_when_a_real_mic_is_the_default(monkeypatch):
    monkeypatch.setattr(D, "list_devices", lambda: [
        D.Device(REAL.name, "r", "microphone", is_default=True),
        D.Device(MIX.name, "m", "microphone")])
    assert D.REALTEK_DRIVER_URL not in D.describe()


# --- 녹음: 스테레오 믹스 트랙 --------------------------------------------------

def test_recorder_adds_a_stereomix_track(tmp_path, monkeypatch):
    from syncnote.recorder import audio as A

    started = []

    class FakeTrack:
        def __init__(self, name, source, path, **kw):
            self.name, self.source, self.path = name, source, path

        def start(self):
            started.append((self.name, self.source.name))

    monkeypatch.setattr(A, "AudioTrack", FakeTrack)
    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX, REAL]))
    rec = A.AudioRecorder(out_dir=tmp_path, system_enabled=False, mic_source=REAL)
    rec.start("x")
    assert started == [("mic", REAL.name), ("stereomix", MIX.name)]

    # 사람이 mic_device 에 스테레오 믹스를 적어 마이크로 쓰면 같은 장치를 또 열지 않음
    started.clear()
    rec = A.AudioRecorder(out_dir=tmp_path, system_enabled=False, mic_source=MIX)
    rec.start("x")
    assert started == [("mic", MIX.name)]

    started.clear()
    rec = A.AudioRecorder(out_dir=tmp_path, system_enabled=False, mic_source=REAL,
                          stereo_mix_enabled=False)
    rec.start("x")
    assert started == [("mic", REAL.name)]


# --- 송출: 마이크 입력이 없으면 스테레오 믹스로 -----------------------------------

def test_stream_uses_the_mix_instead_of_retrying_forever(monkeypatch, caplog):
    from syncnote.stream import live

    monkeypatch.setattr(D, "_sc", lambda: _SC(MIX, [MIX]))
    sess = live.Session.__new__(live.Session)
    sess.settings = types.SimpleNamespace(codec="mp3", bitrate_kbps=64, samplerate=48000,
                                          channels=2, capture="soundcard", source="mic",
                                          server="builtin")
    monkeypatch.setattr("syncnote.stream.capture.resolve_capture", lambda _c: "soundcard")
    monkeypatch.setattr("syncnote.recorder.ffmpeg_util.find_ffmpeg", lambda: "ffmpeg")
    with caplog.at_level("ERROR", logger="syncnote"):
        _cmd, pcm = sess._encoder_cmd("")
    assert pcm is MIX
    assert D.REALTEK_DRIVER_URL in caplog.text


# --- 합치기: 마이크와 겹쳐서 ---------------------------------------------------

need_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg 가 없습니다")


@pytest.fixture
def wavs(tmp_path):
    out = {}
    for name, freq in (("system", 440), ("mic", 880), ("stereomix", 1320)):
        p = tmp_path / f"x.{name}.wav"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency={freq}:sample_rate=48000", "-t", "3", "-ac", "2",
                        str(p)], check=True)
        out[name] = p
    return out


def _titles(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream_tags=title", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    return [ln for ln in out.strip().splitlines() if ln]


@need_ffmpeg
@pytest.mark.parametrize("layout,titles", [
    ("separate", ["시스템 소리", "마이크"]),
    ("both", ["시스템 소리", "마이크", "합친 소리"]),
    ("mixed", ["합친 소리"]),
])
def test_stereomix_is_mixed_into_the_mic_track(wavs, tmp_path, layout, titles):
    from syncnote.recorder.mux import MuxInput, mux

    out = tmp_path / f"o_{layout}.mka"
    mux(out, None, 0.0, [MuxInput(wavs[n], n, 1000.0) for n in ("system", "mic", "stereomix")],
        layout=layout, delete_audio=False)
    assert _titles(out) == titles


@need_ffmpeg
def test_stereomix_takes_the_mic_place_when_there_is_no_mic(wavs, tmp_path):
    from syncnote.recorder.mux import MuxInput, mux

    out = tmp_path / "o.mka"
    mux(out, None, 0.0, [MuxInput(wavs[n], n, 1000.0) for n in ("system", "stereomix")],
        layout="separate", delete_audio=False)
    assert _titles(out) == ["시스템 소리", "마이크"]


# --- 메인보드 드라이버 검색(사용자 요청: 베이스보드 제조업체를 확인해 Google 검색을 Chromium 으로) ---

from syncnote.recorder import driver_help as H  # noqa: E402


@pytest.mark.parametrize("maker,product,label", [
    ("Gigabyte Technology Co., Ltd.", "H410M HD3P", "Gigabyte H410M HD3P"),
    # 강의실 PC 실제 값: 제조사 칸과 모델의 상표가 다름 — 모델을 믿음
    ("ASUSTeK COMPUTER INC.", "Gigabyte B360M DS3H", "Gigabyte B360M DS3H"),
    ("SAMSUNG ELECTRONICS CO., LTD.", "NT960XHA-KC51S", "SAMSUNG NT960XHA-KC51S"),
])
def test_board_label_and_search(maker, product, label):
    assert H.board_label(maker, product) == label
    url = H.search_url(maker, product)
    assert url.startswith("https://www.google.com/search?q=")
    assert label.replace(" ", "+") in url and "Realtek" in url


def test_unknown_board_still_gives_a_generic_search():
    assert "Realtek" in H.search_url("", "")


def test_guidance_names_the_board_and_the_search():
    text = D.driver_guidance(MIX.name, ("Gigabyte Technology Co., Ltd.", "H410M HD3P"))
    assert "Gigabyte H410M HD3P" in text and "msinfo32" in text
    assert H.search_url("Gigabyte Technology Co., Ltd.", "H410M HD3P") in text
    assert D.REALTEK_DRIVER_URL in text


def test_chromium_is_preferred_and_the_newest_is_used(tmp_path, monkeypatch):
    for ver in ("1208", "1243"):
        exe = tmp_path / f"chromium-{ver}" / "chrome-win64" / "chrome.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    shell = tmp_path / "chromium_headless_shell-1300" / "chrome-win64" / "chrome.exe"
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    assert H.chromium_exe() == tmp_path / "chromium-1243" / "chrome-win64" / "chrome.exe"

    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    assert H._real_open_search("https://www.google.com/search?q=x") == "Chromium"
    assert launched[0][0].endswith("chrome.exe") and launched[0][-1].endswith("q=x")


def test_people_run_commands_open_the_search_but_json_does_not(monkeypatch, capsys):
    from syncnote import cli

    opened = []
    monkeypatch.setattr(H, "open_search", lambda url: opened.append(url) or "Chromium")
    monkeypatch.setattr(D, "mix_only_help", lambda board=True: ("안내", "https://g/q"))
    assert cli._print_mix_guidance() is True
    assert opened == ["https://g/q"] and "Chromium 에 드라이버 검색을 열었습니다" in capsys.readouterr().out
    opened.clear()
    assert cli._print_mix_guidance(open_browser=False) is True and opened == []
