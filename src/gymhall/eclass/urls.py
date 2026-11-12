"""eClass URL 지도.

실제 페이지 덤프에서 추출한 값입니다. 이 LMS(메디오피아테크)는
좌측 메뉴만 평범한 GET 링크이고, 게시글 열람과 첨부 다운로드는
JS 가 숨은 <form> 을 POST 하는 방식입니다.

따라서:
  - 메뉴 페이지로 '가는' 것은 여기 URL 을 그대로 씁니다.
  - 게시글/첨부를 '여는' 것은 브라우저에서 해당 요소를 클릭합니다.
    (crawler.py 참고)

menuId 는 사이트 전역 메뉴 정의라서 과목이 달라도 동일합니다.
학교가 LMS 를 개편하면 이 값이 바뀝니다. 그때는
  python -m gymhall eclass probe --menus
로 다시 뽑아 config.toml 의 [eclass.menu_ids] 에 덮어쓰면 됩니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

# --- 메뉴 그룹(부모) ---------------------------------------------------
# 전부 실제 페이지의 좌측 메뉴(<ul id="leftMenu">)에서 그대로 옮긴 값입니다.
# 추측한 값은 하나도 없습니다.
PARENT_CURRICULUM = "menu_00091"   # 학습 목차
PARENT_INFO = "menu_00048"         # 학습 정보
PARENT_ACTIVITY = "menu_00104"     # 학습 활동
PARENT_PROGRESS = "menu_00101"     # 학습 현황
PARENT_COURSE_INFO = "menu_00047"  # 과목 정보
PARENT_GRADE = "menu_00105"        # 성적

# --- 개별 메뉴 ---------------------------------------------------------
# 학습 목차
MENU_CURRICULUM = "menu_00092"     # 학습 목차(주차별)
MENU_LECTURE_LIST = "menu_00099"   # 강의 목록 / 교안
# 학습 활동
MENU_ATTENDANCE = "menu_00082"     # 출석
MENU_ASSIGNMENT = "menu_00063"     # 과제
MENU_DISCUSSION = "menu_00065"     # 토론   (위험)
MENU_EXAM = "menu_00067"           # 시험   (위험)
MENU_SURVEY = "menu_00069"         # 설문   (위험)
MENU_TEAM = "menu_00071"           # 팀활동
# 학습 현황 / 과목 정보 / 성적
MENU_PROGRESS = "menu_00100"       # 학습 현황
MENU_SYLLABUS = "menu_00052"       # 수업 계획서
MENU_COURSE_INFO = "menu_00053"    # 과목 정보
MENU_GRADE = "menu_00106"          # 성적보기
# 학습 정보(게시판 3종은 같은 menuId 를 쓰고 boardClass 로 구분)
MENU_BOARD = "menu_00236"

# 강의실 홈 '더보기' 로 게시판에 들어갈 때만 쓰이는 별도 메뉴 쌍.
# 같은 자료실인데 진입 경로에 따라 menuId 가 달라집니다
# (ViewBoardContent 의 boardClass === 'pds' 분기).
PARENT_PDS_ALT = "menu_00291"
MENU_PDS_ALT = "menu_00292"

# 게시판 종류 -> (boardClass, boardInfoId 접미사)
# 실제로 게시판을 고르는 것은 boardInfoId 의 접미사(-N/-Q/-P)입니다.
# boardClass 는 화면/메뉴 분기에만 쓰여서, 사이트 자체도 일관되지 않습니다
# (강의실 홈의 자료실 '더보기' 는 pds 대신 'notice' 를 넘깁니다).
BOARD_KINDS: dict[str, tuple[str, str]] = {
    "notice": ("notice", "N"),      # 공지사항
    "qna": ("qna", "Q"),            # 질의응답
    "materials": ("pds", "P"),      # 강의자료실 / 학습자료실
}

# 쪽지함 종류
MESSAGE_BOXES = ("RECEIVE", "SEND", "WASTEBASKET")

# 위험 메뉴: 열람 자체가 LMS 기록에 남을 수 있는 것들
RISKY_MENUS = {"exam", "survey", "discussion"}

# 내부 키 -> 사람이 읽는 이름(폴더명으로도 쓰임)
MENU_LABELS: dict[str, str] = {
    "lecture_list": "강의목록",
    "curriculum": "학습목차",
    "syllabus": "강의계획서",
    "course_info": "과목정보",
    "notice": "공지사항",
    "qna": "질의응답",
    "materials": "강의자료실",
    "assignment": "과제",
    "attendance": "출석",
    "team": "팀활동",
    "grade": "성적",
    "exam": "시험",
    "survey": "설문",
    "discussion": "토론",
    "progress": "학습현황",
    "message": "쪽지",
}

# 좌측 메뉴에서 찾을 때 쓰는 링크 텍스트(과목마다 표기가 조금씩 다름)
MENU_LINK_TEXTS: dict[str, tuple[str, ...]] = {
    "lecture_list": ("강의 목록", "강의목록", "교안"),
    "curriculum": ("학습 목차", "학습목차"),
    "syllabus": ("강의 계획서", "강의계획서", "수업 계획서", "수업계획서"),
    "course_info": ("과목 정보", "과목정보"),
    "notice": ("공지사항",),
    "qna": ("질의응답",),
    "materials": ("강의자료실", "학습자료실", "자료실"),
    "assignment": ("과제",),
    "attendance": ("출석",),
    "team": ("팀활동", "팀 활동"),
    "grade": ("성적보기", "성적 보기", "성적"),
    "exam": ("시험",),
    "survey": ("설문",),
    "discussion": ("토론",),
    "progress": ("학습 현황", "학습현황"),
    "message": ("쪽지",),
}


@dataclass(frozen=True)
class Site:
    """base_url 하나로 모든 주소를 만들어 줍니다."""

    base_url: str = "https://eclass.kunsan.ac.kr"

    def _u(self, path: str, params: dict[str, str]) -> str:
        return f"{self.base_url.rstrip('/')}{path}?{urlencode(params)}"

    # -- 진입 --------------------------------------------------------
    @property
    def home(self) -> str:
        return self._u("/Main.do", {"cmd": "viewHome", "userDTO.localeKey": "ko"})

    @property
    def login(self) -> str:
        return f"{self.base_url.rstrip('/')}/Main.do"

    @property
    def logout(self) -> str:
        return self._u("/User.do", {"cmd": "logoutUser"})

    @property
    def my_courses(self) -> str:
        """내 강의실 목록 페이지."""
        return self._u(
            "/Main.do",
            {"cmd": "moveMenu", "mainDTO.parentMenuId": "menu_00026", "mainDTO.menuId": "menu_00030"},
        )

    # -- 쪽지 --------------------------------------------------------
    def messages(self, box: str = "RECEIVE", page: int = 1) -> str:
        """쪽지함. 게시판과 달리 페이지 이동이 평범한 GET 입니다."""
        box = box.upper()
        if box not in MESSAGE_BOXES:
            raise ValueError(f"알 수 없는 쪽지함: {box}")
        params = {"cmd": "viewMessageList", "messageType": box}
        if page > 1:
            params["curPage"] = str(page)
        return self._u("/Message.do", params)

    # -- 과목 --------------------------------------------------------
    def course_home(self, course_id: str) -> str:
        return self._u(
            "/Course.do",
            {
                "cmd": "viewStudyHome",
                "boardInfoDTO.boardInfoGubun": "study_home",
                "courseDTO.courseId": course_id,
                "gubun": "study_course",
            },
        )

    def open_course_home(self, course_id: str) -> str:
        """openType == 'open' 인 공개강좌는 진입점이 다릅니다."""
        return self._u(
            "/Lesson.do",
            {
                "cmd": "viewLessonContentsList",
                "courseDTO.courseId": course_id,
                "pageGubun": "study_home",
                "type": "U",
                "boardInfoDTO.boardInfoGubun": "openkyoan",
                "gubun": "study_course",
            },
        )

    def course_menu(self, course_id: str, parent_menu_id: str, menu_id: str) -> str:
        """좌측 메뉴 항목으로 이동하는 GET 링크."""
        return self._u(
            "/Main.do",
            {
                "cmd": "moveCourseMenu",
                "mainDTO.parentMenuId": parent_menu_id,
                "courseDTO.courseId": course_id,
                "fronGubun": "A",
                "mainDTO.menuId": menu_id,
            },
        )

    def board(self, course_id: str, kind: str) -> str:
        """학습 정보의 게시판(공지/질의응답/자료실)."""
        if kind not in BOARD_KINDS:
            raise ValueError(f"알 수 없는 게시판 종류: {kind}")
        board_class, suffix = BOARD_KINDS[kind]
        # 실제 좌측 메뉴 링크와 글자 하나까지 같게 맞춘 주소입니다.
        # boardInfoGubun 과 boardType 을 빼도 보통은 열리지만, 서버가
        # 둘 중 하나로 화면을 고르는 분기가 있으면 조용히 다른 게시판이
        # 열립니다. 빼서 얻을 것이 없으므로 그대로 둡니다.
        return self._u(
            "/Course.do",
            {
                "cmd": "viewBoardContentsList",
                "boardInfoDTO.boardInfoGubun": board_class,
                "boardInfoDTO.boardInfoId": f"{course_id}-{suffix}",
                "boardInfoDTO.boardClass": board_class,
                "boardInfoDTO.boardType": "course",
                "courseDTO.courseId": course_id,
                "mainDTO.parentMenuId": PARENT_INFO,
                "mainDTO.menuId": MENU_BOARD,
            },
        )

    # -- 메뉴 키 -> URL ----------------------------------------------
    def menu_url(self, course_id: str, key: str, overrides: dict[str, str] | None = None) -> str | None:
        """내부 메뉴 키로 직접 URL 을 만듭니다.

        모르는 키는 None 을 돌려줍니다. 그런 메뉴는 crawler 가 좌측 메뉴
        링크를 '클릭'해서 들어갑니다.

        2026-11 덤프의 <ul id="leftMenu"> 에서 15개 메뉴의 주소를 전부
        확인했으므로, 이제 클릭 경로로 빠지는 메뉴는 없습니다. 클릭 경로는
        학교가 menuId 를 바꿨을 때를 위한 대비책으로만 남깁니다.
        """
        overrides = overrides or {}
        mid = overrides.get(key)

        if key in BOARD_KINDS:
            return self.board(course_id, key)

        table = {
            "curriculum": (PARENT_CURRICULUM, MENU_CURRICULUM),
            "lecture_list": (PARENT_CURRICULUM, MENU_LECTURE_LIST),
            "attendance": (PARENT_ACTIVITY, MENU_ATTENDANCE),
            "assignment": (PARENT_ACTIVITY, MENU_ASSIGNMENT),
            "discussion": (PARENT_ACTIVITY, MENU_DISCUSSION),
            "exam": (PARENT_ACTIVITY, MENU_EXAM),
            "survey": (PARENT_ACTIVITY, MENU_SURVEY),
            "team": (PARENT_ACTIVITY, MENU_TEAM),
            "progress": (PARENT_PROGRESS, MENU_PROGRESS),
            "syllabus": (PARENT_COURSE_INFO, MENU_SYLLABUS),
            "course_info": (PARENT_COURSE_INFO, MENU_COURSE_INFO),
            "grade": (PARENT_GRADE, MENU_GRADE),
        }
        if key in table:
            parent, default_menu = table[key]
            return self.course_menu(course_id, parent, mid or default_menu)

        return None


def parse_course_option(value: str) -> tuple[str, str, str] | None:
    """메인 페이지의 <option value="courseId,교수명,openType"> 를 쪼갭니다."""
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) < 3 or not parts[0]:
        return None
    return parts[0], parts[1], parts[2]
