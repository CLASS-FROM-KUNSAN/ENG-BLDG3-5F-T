"""소리 입출력 단계별 점검(scripts\\audio_check.bat → python -m syncnote audio-check).

강의실 PC 에서 직접 했던 시험을 사람이 그대로 해 볼 수 있게 단계로 나눴습니다.

  0. 장치      입력 장치·기본 녹음/재생 장치·메인보드. 스테레오 믹스뿐이면 드라이버 안내.
  1. 무음      8초 동안 모든 입력 + 스피커 루프백을 함께 녹음. 재생이 없으니 루프백은 0 이어야 하고,
               그런데도 입력에 소리가 있으면 그 소리는 입력 단자에서 온 것.
  2. 말하기    10초 동안 마이크에 대고 말하거나 두드림 → 1단계보다 10 dB 넘게 커진 장치가 진짜로
               마이크 소리가 들어오는 장치(가장 확실한 단계).
  3. 들어 보기 2단계 녹음을 증폭해(최대 +60 dB) 스피커로 들려주고 들렸는지 Y/N 을 기록.
  4. 톤        1 kHz(-20 dBFS) 3초 재생 중 녹음 → 그 입력이 스피커 소리도 녹음하는지(스테레오 믹스).
  5. 잡음      백색 잡음(-26 dBFS) 2초 재생 중 녹음 → 입력과 루프백의 교차상관. 디지털 사본보다
               1 ms 넘게 늦은 두 번째 봉우리 = 스피커 소리가 공기를 거쳐 마이크로 들어옴.
               (밤에 강의실 앰프가 꺼져 있으면 없을 수 있음 — 그때는 결론을 내지 않음)
  6. 요약      판정과 수치를 report.txt·report.json 에, 녹음을 WAV(원본·+증폭·정규화)로 남김.

녹음은 이 PC 의 결과 폴더에만 남습니다(올리지 않음). 4·5단계는 스피커로 소리를 냅니다(--no-sound 로 끔).
"""

from __future__ import annotations

import json
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from ..logging_setup import get as get_logger

log = get_logger("recorder.audio_check")

SR = 48000
SPEECH_RISE_DB = 10.0         # 말할 때 이만큼 커지면 '마이크 소리가 들어온다'
ACOUSTIC_MIN_MS = 1.0          # 디지털 사본보다 이만큼 늦어야 공기를 거친 사본으로 봄
ACOUSTIC_MIN_CORR = 0.05
TONE_MIN = 0.01                # 1 kHz 성분이 이보다 크면 재생 소리가 들어온 것
STEPS = (0, 1, 2, 3, 4, 5, 6)
TITLES = {
    0: "장치 확인", 1: "무음 녹음(8초)", 2: "마이크에 말하기(10초)", 3: "증폭해서 들어 보기",
    4: "1 kHz 톤 재생 중 녹음", 5: "잡음 재생 중 녹음(공기 경로 확인)", 6: "결과 요약",
}


# --- 분석(순수 함수) -------------------------------------------------------------

def mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return x.mean(axis=1) if x.ndim > 1 else x


def db(v: float) -> float:
    return round(float(20 * np.log10(max(float(v), 1e-12))), 1)


def levels(x: np.ndarray, gain_db: float = 30.0) -> dict:
    m = mono(x)
    if len(m) == 0:
        return {"seconds": 0.0, "rms_dbfs": -240.0, "peak_dbfs": -240.0, "nonzero_pct": 0.0,
                "peak_after_gain_dbfs": -240.0}
    peak = float(np.abs(m).max())
    return {
        "seconds": round(len(m) / SR, 2),
        "rms_dbfs": db(np.sqrt(np.mean(m ** 2))),
        "peak_dbfs": db(peak),
        "nonzero_pct": round(100 * float(np.count_nonzero(m)) / len(m), 1),
        "peak_after_gain_dbfs": db(min(1.0, peak * 10 ** (gain_db / 20))),
    }


def tone_level(x: np.ndarray, freq: float = 1000.0) -> float:
    m = mono(x)
    n = len(m)
    if n < 64:
        return 0.0
    spec = np.abs(np.fft.rfft(m * np.hanning(n))) / (n / 4)
    f = np.fft.rfftfreq(n, 1 / SR)
    band = (f > freq - 20) & (f < freq + 20)
    return round(float(spec[band].max()), 6) if band.any() else 0.0


def xcorr_peaks(sig: np.ndarray, ref: np.ndarray, *, lo_ms: float = -10, hi_ms: float = 60,
                count: int = 6) -> list[tuple[float, float]]:
    """sig 와 ref 의 정규화 교차상관에서 큰 봉우리들 [(지연 ms, 상관)], 큰 것부터."""
    a, b = mono(sig), mono(ref)
    n = min(len(a), len(b))
    if n < 1000 or not np.any(a[:n]) or not np.any(b[:n]):
        return []
    a, b = a[:n], b[:n]
    size = 1 << int(np.ceil(np.log2(2 * n)))
    xc = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    xc /= (np.linalg.norm(a) * np.linalg.norm(b) + 1e-30)
    lags = np.arange(int(lo_ms * SR / 1000), int(hi_ms * SR / 1000))
    vals = xc[lags % size]
    peaks: list[tuple[int, float]] = []
    for i in np.argsort(-np.abs(vals)):
        if all(abs(lags[i] - p) > SR // 2000 for p, _ in peaks):     # 0.5 ms 이상 떨어진 것만
            peaks.append((int(lags[i]), float(vals[i])))
        if len(peaks) == count:
            break
    return [(round(lag / SR * 1000, 2), round(c, 4)) for lag, c in peaks]


def acoustic_copy(peaks: list[tuple[float, float]]) -> tuple[float, float] | None:
    """가장 큰 봉우리(디지털 사본)보다 1 ms 넘게 늦은 의미 있는 봉우리 = 공기를 거친 사본."""
    if not peaks:
        return None
    d0 = peaks[0][0]
    for lag, c in peaks[1:]:
        if lag - d0 >= ACOUSTIC_MIN_MS and abs(c) >= ACOUSTIC_MIN_CORR:
            return round(lag - d0, 2), c
    return None


def normalize(x: np.ndarray, peak_db: float = -3.0, max_gain_db: float = 60.0) -> tuple[np.ndarray, float]:
    """최대치를 peak_db 로 맞춤(최대 max_gain_db 만큼). (증폭본, 적용한 dB)."""
    x = np.asarray(x, dtype=np.float64)
    peak = float(np.abs(x).max()) if x.size else 0.0
    if peak <= 0:
        return x, 0.0
    gain_db = min(max_gain_db, peak_db - 20 * np.log10(peak))
    return np.clip(x * 10 ** (gain_db / 20), -1, 1), round(float(gain_db), 1)


def save_wav(path: Path, x: np.ndarray) -> None:
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name)[:40].strip("_") or "device"


# --- 실제 장치(soundcard) -------------------------------------------------------

class SoundcardIO:
    """입력 전부 + 기본 스피커 루프백을 동시에 녹음하고, 기본 스피커로 재생."""

    def __init__(self) -> None:
        import soundcard as sc  # type: ignore

        self.sc = sc
        self.speaker = sc.default_speaker()

    def describe(self) -> dict:
        sc = self.sc
        try:
            default_mic = sc.default_microphone().name
        except Exception:  # noqa: BLE001 — 기본 녹음 장치 없음
            default_mic = ""
        return {"default_mic": default_mic, "default_speaker": self.speaker.name,
                "inputs": [m.name for m in sc.all_microphones(include_loopback=False)]}

    def _targets(self):
        mics = self.sc.all_microphones(include_loopback=True)
        ins = [(m.name, m) for m in mics if not getattr(m, "isloopback", False)]
        lb = [("[루프백] " + m.name, m) for m in mics
              if getattr(m, "isloopback", False) and m.name == self.speaker.name]
        return ins + lb[:1]

    def record(self, seconds: float, play: np.ndarray | None = None) -> dict[str, np.ndarray]:
        targets = self._targets()
        out: dict[str, np.ndarray] = {}
        errors: dict[str, str] = {}
        bar = threading.Barrier(len(targets) + 1)

        def rec(key, dev):
            try:
                with dev.recorder(samplerate=SR, channels=2) as r:
                    bar.wait(timeout=15)
                    chunks, end = [], time.time() + seconds
                    while time.time() < end:
                        chunks.append(r.record(numframes=SR // 10))
                out[key] = np.concatenate(chunks) if chunks else np.zeros((0, 2))
            except Exception as exc:  # noqa: BLE001 — 그 장치만 실패로 기록
                errors[key] = f"{type(exc).__name__}: {exc}"
                try:
                    bar.abort()
                except Exception:  # noqa: BLE001
                    pass

        threads = [threading.Thread(target=rec, args=t, daemon=True) for t in targets]
        for t in threads:
            t.start()
        try:
            bar.wait(timeout=15)
        except threading.BrokenBarrierError:
            pass
        if play is not None:
            time.sleep(0.6)
            self.play(play)
        for t in threads:
            t.join(seconds + 20)
        for k, e in errors.items():
            out.setdefault(k, np.zeros((0, 2)))
            log.warning("[%s] 녹음 실패: %s", k, e)
        return out

    def play(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = np.stack([x, x], axis=1)
        self.speaker.play(x, samplerate=SR)


# --- 단계 실행 ------------------------------------------------------------------

@dataclass
class Check:
    io: object
    out_dir: Path
    ask: object = input                  # 사람에게 묻기(테스트는 가짜)
    say: object = print
    auto: bool = False                   # 묻지 않고 진행(원격에서 실행할 때). 3단계는 건너뜀
    no_sound: bool = False               # 3·4·5단계(스피커로 소리 냄)를 건너뜀
    steps: tuple = STEPS
    gain_db: float = 30.0
    silence_sec: float = 8.0
    speech_sec: float = 10.0
    report: dict = field(default_factory=dict)
    verdicts: list = field(default_factory=list)

    # 사람에게 묻기: Enter = 진행, s = 건너뛰기, q = 끝내기
    def _go(self, step: int, how: str) -> str:
        self.say("")
        self.say(f"[{step}/6] {TITLES[step]}")
        self.say(f"      {how}")
        if self.auto:
            return "go"
        try:
            a = str(self.ask("      Enter = 진행, s = 건너뛰기, q = 끝내기: ")).strip().lower()
        except (EOFError, OSError):
            return "quit"
        return {"s": "skip", "q": "quit"}.get(a, "go")

    def _record(self, tag: str, seconds: float, play=None) -> dict[str, np.ndarray]:
        data = self.io.record(seconds, play)
        for key, x in data.items():
            base = f"{tag}_{_safe(key)}"
            save_wav(self.out_dir / f"{base}.wav", x)
            save_wav(self.out_dir / f"{base}_plus{int(self.gain_db)}dB.wav",
                     mono(x) * 10 ** (self.gain_db / 20))
        return data

    def run(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.report = {"time": datetime.now().isoformat(timespec="seconds"), "steps": {}}
        silence = speech = None
        for step in self.steps:
            try:
                if step == 0:
                    self._step0()
                elif step == 1:
                    act = self._go(1, f"주변을 조용히 하고, 아무것도 재생하지 마세요. {self.silence_sec:g}초 녹음합니다.")
                    if act == "quit":
                        break
                    if act == "go":
                        silence = self._record("1_silence", self.silence_sec)
                        self._report_levels(1, silence)
                elif step == 2:
                    act = self._go(2, f"Enter 를 누른 뒤 {self.speech_sec:g}초 동안 마이크에 대고 말하거나 "
                                      "손가락으로 마이크를 톡톡 두드리세요.")
                    if act == "quit":
                        break
                    if act == "go":
                        speech = self._record("2_speech", self.speech_sec)
                        self._report_levels(2, speech)
                        self._judge_speech(silence, speech)
                elif step == 3:
                    if self.no_sound or self.auto or not speech:
                        self.report["steps"]["3"] = {"skipped": "소리 끔·자동 실행·2단계 없음"}
                        continue
                    act = self._go(3, "2단계 녹음을 크게 키워 스피커로 들려줍니다.")
                    if act == "quit":
                        break
                    if act == "go":
                        self._step3(speech)
                elif step == 4:
                    if self.no_sound:
                        self.report["steps"]["4"] = {"skipped": "소리 끔"}
                        continue
                    act = self._go(4, "스피커로 1 kHz 소리를 3초 냅니다(조금 큼). 그동안 녹음합니다.")
                    if act == "quit":
                        break
                    if act == "go":
                        self._step4(silence)
                elif step == 5:
                    if self.no_sound:
                        self.report["steps"]["5"] = {"skipped": "소리 끔"}
                        continue
                    act = self._go(5, "스피커로 '쉬—' 하는 잡음을 2초 냅니다. 그동안 녹음합니다.")
                    if act == "quit":
                        break
                    if act == "go":
                        self._step5()
                elif step == 6:
                    self._step6()
            except Exception as exc:  # noqa: BLE001 — 한 단계가 실패해도 다음 단계로
                log.exception("점검 %d단계 실패", step)
                self.report["steps"][str(step)] = {"error": f"{type(exc).__name__}: {exc}"}
                self.say(f"      ! {step}단계 실패: {exc}")
        if 6 in self.steps and "6" not in self.report["steps"]:
            self._step6()
        return self.report

    # -- 단계별 ----------------------------------------------------------------
    def _step0(self) -> None:
        self.say("")
        self.say("[0/6] 장치 확인")
        info = self.io.describe()
        from . import devices, driver_help

        info["board"] = " ".join(x for x in driver_help.baseboard() if x)
        for k, v in (("기본 녹음 장치", info.get("default_mic") or "(없음)"),
                     ("기본 재생 장치", info.get("default_speaker")), ("메인보드", info["board"] or "?")):
            self.say(f"      {k}: {v}")
        for name in info.get("inputs", []):
            mark = "  (스테레오 믹스)" if devices.is_mix_device(name) else ""
            self.say(f"      입력: {name}{mark}")
        if devices.mix_only(info.get("default_mic", ""), info.get("inputs", [])):
            guide = devices.driver_guidance(info.get("default_mic", ""), driver_help.baseboard())
            self.verdicts.append("기본 녹음 장치가 스테레오 믹스뿐 — Realtek 드라이버 설치·업데이트가 필요합니다.")
            for line in guide.splitlines():
                self.say("      " + line)
        self.report["steps"]["0"] = info

    def _report_levels(self, step: int, data: dict) -> None:
        rows = {k: levels(x, self.gain_db) for k, x in data.items()}
        self.report["steps"][str(step)] = {"levels": rows}
        for k, r in rows.items():
            self.say(f"      {k}: RMS {r['rms_dbfs']} dBFS, 최대 {r['peak_dbfs']} dBFS, "
                     f"+{self.gain_db:g} dB 하면 최대 {r['peak_after_gain_dbfs']} dBFS")
        if step == 1:
            for k, r in rows.items():
                if k.startswith("[루프백]"):
                    continue
                lb = next((v for kk, v in rows.items() if kk.startswith("[루프백]")), None)
                if r["nonzero_pct"] == 0:
                    msg = (f"'{k}' 는 신호가 정확히 0 입니다 — 가상 장치이거나 마이크 권한이 꺼져 "
                           "있거나 아무것도 연결되지 않았습니다.")
                    self.say("      → " + msg)
                    self.verdicts.append(msg)
                elif lb is None or lb["rms_dbfs"] < -120:
                    self.say(f"      → '{k}' 에 재생이 아닌 소리(입력 단자)가 들어옵니다.")

    def _judge_speech(self, silence, speech) -> None:
        if not silence:
            self.say("      (1단계를 건너뛰어 비교하지 못했습니다)")
            return
        found = []
        res = {}
        for k, x in speech.items():
            if k.startswith("[루프백]") or k not in silence:
                continue
            rise = levels(x)["rms_dbfs"] - levels(silence[k])["rms_dbfs"]
            res[k] = round(rise, 1)
            if rise >= SPEECH_RISE_DB:
                found.append(k)
        self.report["steps"]["2"]["rise_db"] = res
        for k, r in res.items():
            self.say(f"      {k}: 1단계보다 {r:+.1f} dB")
        if found:
            msg = "마이크 소리가 들어오는 장치: " + ", ".join(found)
        else:
            msg = (f"말해도 {SPEECH_RISE_DB:g} dB 넘게 커진 입력이 없습니다 — 마이크 소리가 이 PC 로 "
                   "들어오지 않거나 아주 작습니다(연결·단자·드라이버 확인).")
        self.verdicts.append(msg)
        self.say("      → " + msg)

    def _step3(self, speech) -> None:
        cands = {k: x for k, x in speech.items() if not k.startswith("[루프백]") and len(x)}
        if not cands:
            return
        key = max(cands, key=lambda k: levels(cands[k])["rms_dbfs"])
        loud, gain = normalize(mono(cands[key]))
        save_wav(self.out_dir / f"3_listen_{_safe(key)}_normalized.wav", loud)
        self.say(f"      '{key}' 녹음을 {gain:+.1f} dB 키워 재생합니다…")
        self.io.play(loud)
        try:
            heard = str(self.ask("      들렸나요? (Y/N): ")).strip().lower() in ("y", "yes", "예", "ㅛ")
        except (EOFError, OSError):
            heard = None
        self.report["steps"]["3"] = {"device": key, "gain_db": gain, "heard": heard}
        if heard is not None:
            self.verdicts.append(f"증폭({gain:+.1f} dB)해서 들어 봄: {'들림' if heard else '안 들림'} ({key})")

    def _step4(self, silence) -> None:
        t = np.arange(int(SR * 3)) / SR
        tone = (0.1 * np.sin(2 * np.pi * 1000 * t)).astype("float32")
        data = self._record("4_tone", 5.0, play=np.stack([tone, tone], axis=1))
        res = {}
        for k, x in data.items():
            res[k] = {"tone_1k": tone_level(x), **levels(x, self.gain_db)}
        self.report["steps"]["4"] = res
        for k, r in res.items():
            if k.startswith("[루프백]"):
                continue
            hit = r["tone_1k"] >= TONE_MIN
            self.say(f"      {k}: 1 kHz {'들어옴' if hit else '없음'} ({r['tone_1k']})")
            if hit:
                self.verdicts.append(f"'{k}' 는 스피커로 나가는 소리도 녹음합니다.")

    def _step5(self) -> None:
        rng = np.random.default_rng(1)
        burst = (rng.standard_normal(SR * 2) * 0.05).clip(-0.2, 0.2).astype("float32")
        data = self._record("5_noise", 4.0, play=np.stack([burst, burst], axis=1))
        ref = next((x for k, x in data.items() if k.startswith("[루프백]")), None)
        res = {}
        if ref is None or not np.any(mono(ref)):
            self.say("      스피커 루프백을 받지 못해 비교할 수 없습니다.")
            self.report["steps"]["5"] = {"error": "루프백 없음"}
            return
        for k, x in data.items():
            if k.startswith("[루프백]"):
                continue
            peaks = xcorr_peaks(x, ref)
            ac = acoustic_copy(peaks)
            res[k] = {"peaks_ms_corr": peaks, "acoustic_delay_ms": ac[0] if ac else None}
            if ac:
                msg = (f"'{k}' 에 스피커 소리가 {ac[0]} ms 늦게(공기를 거쳐) 들어옵니다 — 이 입력에 "
                       "살아 있는 마이크가 있습니다.")
            elif peaks and abs(peaks[0][1]) >= 0.3:
                msg = (f"'{k}' 에는 재생 소리의 디지털 사본만 있고 공기를 거친 사본은 없습니다 "
                       "(스피커·앰프가 꺼져 있거나 마이크가 멀면 그럴 수 있음 — 2단계 결과를 믿으세요).")
            else:
                msg = f"'{k}' 에 재생 소리가 들어오지 않았습니다."
            self.say("      → " + msg)
            self.verdicts.append(msg)
        self.report["steps"]["5"] = res

    def _step6(self) -> None:
        self.report["verdicts"] = list(self.verdicts)
        (self.out_dir / "report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")
        lines = [f"소리 입출력 점검 {self.report['time']}", ""] + [f"- {v}" for v in self.verdicts]
        (self.out_dir / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.report["steps"]["6"] = {"saved": str(self.out_dir)}
        self.say("")
        self.say("[6/6] 결과 요약")
        for v in self.verdicts or ["(판정할 단계를 하지 않았습니다)"]:
            self.say(f"      - {v}")
        self.say(f"      결과·녹음(WAV) 폴더: {self.out_dir}")
