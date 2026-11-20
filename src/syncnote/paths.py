"""산출물 폴더 구조와 안전한 파일명 처리."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

# eclass/<학기>/ 바로 아래에서 '과목이 아닌' 폴더 전부. 크롤러는 계정 단위
# 수집물을 반드시 이 이름으로만 씁니다. 업로드는 이 목록으로 과목(main)과
# 비과목(sub)을 가르므로, 새 계정 단위 폴더를 만들면 여기에 먼저 추가하세요.
# (tests 가 크롤러 소스에서 목록 밖 '_' 폴더를 찾으면 실패합니다.)
ECLASS_ACCOUNT_DIR = "_계정"
ECLASS_MESSAGE_DIR = "_쪽지"
ECLASS_NON_COURSE_DIRS = frozenset({ECLASS_ACCOUNT_DIR, ECLASS_MESSAGE_DIR})

# Windows 에서 쓸 수 없는 문자 + 제어문자
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Windows 예약 이름
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, max_len: int = 120, fallback: str = "untitled") -> str:
    """임의의 문자열을 Windows/Linux 양쪽에서 안전한 파일명으로 바꿉니다.

    한글은 그대로 둡니다(NFC 정규화). 길이는 바이트가 아니라 문자 기준입니다.
    """
    name = unicodedata.normalize("NFC", str(name))
    name = _ILLEGAL.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return fallback
    stem = name.split(".")[0].upper()
    if stem in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        # 확장자는 살립니다
        if "." in name[-8:]:
            stem, _, ext = name.rpartition(".")
            name = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            name = name[:max_len].rstrip(" .")
    return name or fallback


def unique_path(path: Path) -> Path:
    """같은 이름이 있으면 ' (2)', ' (3)' 을 붙여 충돌을 피합니다."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} ({datetime.now():%Y%m%d%H%M%S}){suffix}"


class Layout:
    """data_dir 아래 폴더 구조를 한 곳에서 관리합니다.

    <data_dir>/
      eclass/<학기>/<과목>/<메뉴>/...
      recordings/<YYYY-MM-DD>/<세션이름>/
      transcripts/<YYYY-MM-DD>/<세션이름>/
      logs/
      state/
      tmp/
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # -- 최상위 ------------------------------------------------------
    @property
    def eclass(self) -> Path:
        return self.root / "eclass"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def transcripts(self) -> Path:
        return self.root / "transcripts"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    # -- 하위 --------------------------------------------------------
    def course_dir(self, term: str, course: str) -> Path:
        return self.eclass / safe_name(term) / safe_name(course)

    def menu_dir(self, term: str, course: str, menu: str) -> Path:
        return self.course_dir(term, course) / safe_name(menu)

    def _dated(self, root: Path, started: datetime, course, label: str) -> Path:
        """<뿌리>/<날짜>[/<과목>]/<이름>.

        **course 가 None 이면 과목 칸이 아예 없습니다.** '_미지정' 같은
        폴더를 만들면 이미 있던 녹화 폴더와 경로가 어긋나고, 옛 세션을
        다시 올리거나 찾을 때 길이 끊깁니다. 과목을 아는 녹화만
        한 칸 깊어집니다.
        """
        base = root / f"{started:%Y-%m-%d}"
        name = getattr(course, "name", "") if course is not None else ""
        if name:
            base = base / safe_name(name)
        return base / safe_name(label)

    def session_dir(self, started: datetime, course=None, label: str = "") -> Path:
        return self._dated(self.recordings, started, course, label)

    def transcript_dir(self, started: datetime, course=None, label: str = "") -> Path:
        return self._dated(self.transcripts, started, course, label)

    def ensure(self) -> None:
        for p in (self.eclass, self.recordings, self.transcripts, self.logs, self.state, self.tmp):
            p.mkdir(parents=True, exist_ok=True)
