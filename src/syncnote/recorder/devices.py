"""오디오 장치 목록.

장치 이름을 잘못 적어 3시간을 무음으로 녹음하는 사고가 가장 흔합니다.
녹화를 걸기 전에 반드시 `python -m syncnote devices` 로 확인하세요.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ..logging_setup import get as get_logger

log = get_logger("recorder.devices")


# '안 넘겼다' 와 '없다(None)' 를 가르는 표시입니다.
_UNSET = object()


class SoundcardMissing(Exception):
    def __str__(self) -> str:
        return (
            "soundcard 패키지가 없습니다.  pip install soundcard\n"
            "이 패키지는 Windows 의 WASAPI 루프백을 직접 사용하므로\n"
            "'스테레오 믹스'나 VB-Cable 같은 가상 드라이버를 설치하지 않아도\n"
            "PC 에서 나는 소리를 그대로 녹음할 수 있습니다."
        )


class NoMicrophone(Exception):
    """녹음 장치(입력)가 하나도 없음."""


# '스테레오 믹스' 는 마이크입니다. 메인보드 마이크 단자에 꽂은 마이크가 Realtek 드라이버에서 이
# 이름으로 보입니다(강의실 PC). 예전에는 '스피커 소리를 다시 녹음하는 장치' 로 보고 마이크에서
# 빼서, 마이크가 이것뿐인 PC 는 마이크 녹음을 하지 않았고 송출(source = "mic")은 시작조차
# 못 했습니다. 이제 다른 마이크와 똑같이 다룹니다(이름으로 거르지 않음).


@dataclass
class Device:
    name: str
    id: str
    kind: Literal["speaker", "microphone", "loopback"]
    channels: int = 2
    is_default: bool = False

    def __str__(self) -> str:
        mark = " (기본)" if self.is_default else ""
        return f"[{self.kind}] {self.name}{mark}"


def _sc():
    try:
        import soundcard  # type: ignore
    except ImportError as exc:
        raise SoundcardMissing() from exc
    return soundcard


def list_devices() -> list[Device]:
    sc = _sc()
    out: list[Device] = []

    try:
        default_spk = sc.default_speaker()
        default_spk_name = default_spk.name
    except Exception:
        default_spk_name = None
    try:
        default_mic_name = sc.default_microphone().name
    except Exception:
        default_mic_name = None

    for spk in sc.all_speakers():
        out.append(Device(spk.name, str(spk.id), "speaker",
                          getattr(spk, "channels", 2), spk.name == default_spk_name))
    for mic in sc.all_microphones(include_loopback=True):
        kind: Literal["microphone", "loopback"] = (
            "loopback" if getattr(mic, "isloopback", False) else "microphone"
        )
        out.append(Device(mic.name, str(mic.id), kind,
                          getattr(mic, "channels", 2), mic.name == default_mic_name))
    return out


def resolve_system_source(name: str = ""):
    """시스템 소리(스피커로 나가는 소리)를 잡는 루프백 입력을 찾습니다."""
    sc = _sc()
    if name:
        for mic in sc.all_microphones(include_loopback=True):
            if name.lower() in mic.name.lower() and getattr(mic, "isloopback", False):
                return mic
        # 이름이 스피커 이름일 수도 있습니다
        for spk in sc.all_speakers():
            if name.lower() in spk.name.lower():
                return sc.get_microphone(id=str(spk.name), include_loopback=True)
        raise ValueError(
            f"시스템 오디오 장치를 찾지 못했습니다: {name!r}\n"
            "python -m syncnote devices 로 정확한 이름을 확인하세요."
        )
    spk = sc.default_speaker()
    return sc.get_microphone(id=str(spk.name), include_loopback=True)


def resolve_mic_source(name: str = ""):
    sc = _sc()
    if name:
        for mic in sc.all_microphones(include_loopback=False):
            if name.lower() in mic.name.lower():
                return mic
        raise ValueError(
            f"마이크를 찾지 못했습니다: {name!r}\n"
            "python -m syncnote devices 로 정확한 이름을 확인하세요."
        )
    # 이름이 없으면 기본 장치, 없으면 첫 장치('스테레오 믹스' 도 마이크 — 위 설명).
    try:
        default = sc.default_microphone()
    except Exception:  # noqa: BLE001 — 기본 녹음 장치가 아예 없음
        default = None
    if default is not None:
        return default
    mics = list(sc.all_microphones(include_loopback=False))
    if mics:
        return mics[0]
    raise NoMicrophone("녹음 장치(마이크)가 하나도 없습니다. 시스템 소리(와 화면)만 녹음합니다.")


# ---------------------------------------------------------------------
# 마이크 자동 선택
# ---------------------------------------------------------------------
# 2026-11-13 실기 PC 의 기본 마이크는 '마이크 배열 (Audio Share)' 라는
# 가상 장치였고, 61초 녹음에 0 아닌 바이트가 **하나도** 없었습니다.
# 설정을 비워 둔 PC 는 OS 기본 장치를 그대로 쓰는데, 그게 이런
# 장치면 수업 내내 비트 단위 0 이 녹음되고 끝나고서야 압니다.
#
# 여기서 하는 일은 **좋은 마이크를 고르는 것이 아니라 죽은 마이크를
# 거르는 것**입니다. 잡음 크기로 품질을 판단할 수는 없습니다 —
# 인텔이 Focusrite 보다 30배 시끄러운 것은 성능이 아니라 잡음 바닥이
# 다르기 때문입니다. 쓸 수 있는 신호는 "정확히 0 이면 죽은 것" 하나뿐입니다.
#
# 시스템 소리(루프백)에는 **절대 쓰지 않습니다.** 재생 중인 게 없으면
# 정확히 0 이 정상이라 전부 오탐이 됩니다.


@dataclass(frozen=True)
class ProbeResult:
    name: str
    peak: float
    error: str | None = None
    via: str = ""            # "ffmpeg DirectShow" — soundcard 가 못 열어 대체 경로로 잰 경우

    # 이보다 작으면(-60 dBFS) 아무것도 꽂히지 않은 단자의 전기 잡음 수준입니다.
    # 강의실 PC 메인보드 단자(비어 있음) 실측: 최대 -68.7 dB(0.00037).
    FAINT = 1e-3

    @property
    def faint(self) -> bool:
        return self.error is None and 0.0 < self.peak < self.FAINT

    @property
    def dead(self) -> bool:
        """비트 단위로 정확히 0 이거나, 열지 못했습니다.

        진짜 마이크는 조용한 방에서도 ADC 열잡음으로 ±1 LSB 는
        흔들립니다. 정확히 0 이면 조용한 게 아니라 신호 경로가
        없는 것입니다.
        """
        return self.error is not None or self.peak == 0.0


def probe_mic(mic, *, duration_sec: float = 1.5,
              samplerate: int = 48000, channels: int = 2) -> ProbeResult:
    """장치를 잠깐 열어 최대 레벨만 잽니다. 디스크에는 쓰지 않습니다.

    받은 소리는 메모리에서 통계만 세고 버립니다. 수업 전 강의실
    대화가 파일로 남지 않게 하려는 것으로, pre-roll 프라이버시
    원칙과 같습니다.
    """
    import numpy as np

    from .dshow_mic import open_recording

    name = str(getattr(mic, "name", "") or "(이름 없음)")
    peak = 0.0
    via = []
    try:
        with open_recording(mic, samplerate=samplerate, channels=channels, blocksize=4096,
                            on_fallback=lambda _alt, _why: via.append("ffmpeg DirectShow")) as rec:
            deadline = time.monotonic() + float(duration_sec)
            while time.monotonic() < deadline:
                data = rec.record(numframes=4096)
                if data is None or len(data) == 0:
                    continue
                peak = max(peak, float(np.max(np.abs(data))))
    except Exception as exc:
        # 빈 AssertionError 는 str() 이 "" — 원인이 로그에서 사라져 '신호 0' 으로 오인됐음
        return ProbeResult(name, 0.0, str(exc) or type(exc).__name__, "".join(via))
    return ProbeResult(name, peak, None, "".join(via))


_CONSENT = ("Software", "Microsoft", "Windows", "CurrentVersion", "CapabilityAccessManager",
            "ConsentStore", "microphone")


def enable_mic_permission(*, winreg_mod=None) -> tuple[list[str], list[str]]:
    """꺼져 있는 Windows 마이크 권한을 켭니다. (켠 곳, 못 켠 곳과 이유).

    세 곳이 있습니다. 하나라도 Deny 면 데스크톱 앱(이 프로그램)은 마이크를 못 씁니다.
      - 이 PC 전체 '마이크 액세스'      HKLM — 관리자 권한이 있어야 바꿈
      - 이 사용자의 '앱이 마이크에 액세스' HKCU
      - '데스크톱 앱이 마이크에 액세스'    HKCU\\NonPackaged
    그룹 정책(AppPrivacy LetAppsAccessMicrophone = 2, 강제 거부)이 걸려 있으면 바꿔도
    소용없으므로 바꾸지 않고 알립니다. Allow 인 곳은 건드리지 않습니다.
    """
    if winreg_mod is None:
        if sys.platform != "win32":
            return [], []
        import winreg as winreg_mod  # noqa: N813
    w = winreg_mod
    base = "\\".join(_CONSENT)
    changed: list[str] = []
    problems: list[str] = []
    try:
        with w.OpenKey(w.HKEY_LOCAL_MACHINE,
                       "SOFTWARE\\Policies\\Microsoft\\Windows\\AppPrivacy") as k:
            forced, _ = w.QueryValueEx(k, "LetAppsAccessMicrophone")
        if int(forced) == 2:
            return [], ["그룹 정책이 마이크를 강제로 막고 있습니다(LetAppsAccessMicrophone = 2). "
                        "학교 전산 담당자에게 해제를 요청해야 합니다."]
    except OSError:
        pass
    for root, sub, label in ((w.HKEY_LOCAL_MACHINE, base, "이 PC 전체의 마이크 액세스"),
                             (w.HKEY_CURRENT_USER, base, "이 사용자의 앱 마이크 액세스"),
                             (w.HKEY_CURRENT_USER, base + "\\NonPackaged",
                              "데스크톱 앱의 마이크 액세스")):
        try:
            with w.OpenKey(root, sub) as k:
                value, _ = w.QueryValueEx(k, "Value")
        except OSError:
            continue
        if str(value).lower() != "deny":
            continue
        try:
            with w.OpenKey(root, sub, 0, w.KEY_SET_VALUE) as k:
                w.SetValueEx(k, "Value", 0, w.REG_SZ, "Allow")
            changed.append(label)
        except PermissionError:
            problems.append(f"{label}: 관리자 권한이 있어야 켤 수 있습니다 — 관리자로 "
                            "scripts\\setup.bat 을 실행하거나 설정 > 개인 정보 및 보안 > 마이크 에서 켜세요")
        except OSError as exc:
            problems.append(f"{label}: 켜지 못했습니다({exc})")
    return changed, problems


def mic_permission_denied() -> bool | None:
    """Windows 마이크 권한이 꺼져 있나(데스크톱 앱 포함). 모르면 None.

    '전부 0 이면 권한' 이라고 짐작하던 것을, 실제 설정을 읽어 확인합니다
    (강의실 PC 는 권한이 Allow 인데 '권한이 꺼져 있을 가능성' 이라고 안내했음).
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    base = "\\".join(("Software", "Microsoft", "Windows", "CurrentVersion",
                       "CapabilityAccessManager", "ConsentStore", "microphone"))
    found = False
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE, base), (winreg.HKEY_CURRENT_USER, base),
                      (winreg.HKEY_CURRENT_USER, base + "\\NonPackaged")):
        try:
            with winreg.OpenKey(root, sub) as k:
                value, _ = winreg.QueryValueEx(k, "Value")
        except OSError:
            continue
        found = True
        if str(value).lower() == "deny":
            return True
    return False if found else None


def select_working_mic(*, duration_sec: float = 1.5,
                       samplerate: int = 48000, channels: int = 2,
                       mics=_UNSET, default=_UNSET,
                       exclude: Sequence[str] = ()):
    """살아 있는 마이크 하나를 고릅니다. (장치, 시험결과들) 을 돌려줍니다.

    기본 장치를 먼저 시험하고, 죽어 있으면 나머지를 순서대로 봅니다.
    첫 성공에서 멈추므로 보통 1.5초면 끝납니다.

    **전부 죽어 있어도 기본 장치를 돌려줍니다.** 포기하면 영상만
    남는데, 그보다는 시도하고 경고하는 쪽이 낫습니다. 전부 0 이라는
    것은 장치 문제가 아니라 Windows 마이크 권한이 꺼져 있다는
    뜻일 가능성이 큽니다 — 하나만 0 이면 장치, 전부 0 이면 권한.

    exclude 에 적힌 이름(부분 일치)을 가진 장치는 건너뜁니다.

    mics/default 는 테스트 주입용입니다. 실장치 없이 모든 갈래를
    시험할 수 있어야 합니다(리눅스 CI 에서 이 함수가 한 줄도 안 돌면
    의미가 없습니다).
    """
    # None 은 '마이크가 없다' 는 **답**일 수 있습니다. '안 넘겼다' 와
    # 섞으면, 장치가 없는 PC 를 시험하려 할 때마다 진짜 soundcard 를
    # 부르게 됩니다(리눅스 CI 에서는 그 자리에서 터집니다).
    if mics is _UNSET or default is _UNSET:
        sc = _sc()
        if mics is _UNSET:
            mics = list(sc.all_microphones(include_loopback=False))
        if default is _UNSET:
            default = sc.default_microphone()

    excl_set = {str(x).strip().lower() for x in exclude if str(x).strip()}

    def _is_excluded(mic) -> bool:
        if not mic:
            return False
        name = str(getattr(mic, "name", "") or "").lower()
        return any(ex in name for ex in excl_set)

    ordered = []
    seen = set()
    for mic in ([default] if default is not None else []) + list(mics):
        name = str(getattr(mic, "name", "") or "")
        if name in seen or _is_excluded(mic):
            continue
        seen.add(name)
        ordered.append(mic)

    probes: list[ProbeResult] = []
    for mic in ordered:
        result = probe_mic(mic, duration_sec=duration_sec,
                           samplerate=samplerate, channels=channels)
        probes.append(result)
        if not result.dead:
            return mic, probes
    # 전부 죽어 있어도 시도는 합니다(기본 장치, 없으면 첫 장치).
    fallback = None if _is_excluded(default) else default
    if fallback is None and ordered:
        fallback = ordered[0]
    return fallback, probes


def describe() -> str:
    lines = ["사용 가능한 오디오 장치", "=" * 60]
    try:
        devices = list_devices()
    except SoundcardMissing as exc:
        return str(exc)
    for kind, title in (
        ("loopback", "시스템 소리 캡처용 (recording.audio.system_device 에 쓰세요)"),
        ("microphone", "마이크 (recording.audio.mic_device 에 쓰세요)"),
        ("speaker", "재생 장치 (참고용)"),
    ):
        picked = [d for d in devices if d.kind == kind]
        lines.append("")
        lines.append(f"── {title}")
        if not picked:
            lines.append("   (없음)")
        for d in picked:
            lines.append(f'   "{d.name}"{"  <- 기본" if d.is_default else ""}')
    lines.append("")
    lines.append("config.toml 에는 이름의 일부만 적어도 됩니다 (부분 일치).")
    return "\n".join(lines)
