"""오디오 장치 목록.

장치 이름을 잘못 적어 3시간을 무음으로 녹음하는 사고가 가장 흔합니다.
녹화를 걸기 전에 반드시 `python -m gymhall devices` 로 확인하세요.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..logging_setup import get as get_logger

log = get_logger("recorder.devices")


class SoundcardMissing(Exception):
    def __str__(self) -> str:
        return (
            "soundcard 패키지가 없습니다.  pip install soundcard\n"
            "이 패키지는 Windows 의 WASAPI 루프백을 직접 사용하므로\n"
            "'스테레오 믹스'나 VB-Cable 같은 가상 드라이버를 설치하지 않아도\n"
            "PC 에서 나는 소리를 그대로 녹음할 수 있습니다."
        )


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
            "python -m gymhall devices 로 정확한 이름을 확인하세요."
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
            "python -m gymhall devices 로 정확한 이름을 확인하세요."
        )
    return sc.default_microphone()


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
