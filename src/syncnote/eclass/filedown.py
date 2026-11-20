"""eClass 파일 다운로드 링크 해석.

사이트에는 **두 개의 다운로드 함수**가 있습니다. 이름이 비슷하고 인자
모양도 같지만 **주소가 다릅니다.**

    lmsdata/js/FileDown.js
        function fileDown(rfileName, sfileName, filePath) {
            var loc = "fileDown?rFileName=" + ... ;
            hiddenFrame.location.href = loc;
        }

    lmsdata/js/FileDownload.js
        function fileDownload(rfileName, sfileName, filePath) {
            var loc = "fileDownServlet?rFileName=" + ... ;
            hiddenFrame.location.href = loc;
        }

어디에 붙는지:
    강의 목록(교안)  <a class="btn" onclick="fileDown(...)">교안</a>
    게시글 상세 첨부 <span onclick="fileDownload(...)">보고서양식.hwp</span>

세 인자는 이미 서버가 알아보는 형태입니다. 퍼센트 인코딩인데 '%' 자리에
'*' 를 쓴 것뿐입니다(`*EC*9E*A5` = `%EC%9E%A5` = '장'). 주소를 만들 때는
**그대로** 붙이고, 사람이 읽을 이름을 얻을 때만 '*' 를 '%' 로 바꿔
디코딩합니다. 다시 인코딩하면 '*' 가 '%2A' 가 되어 서버가 다른 파일을
찾습니다.

클릭 대신 주소를 직접 만드는 이유:
  - 다운로드가 숨은 iframe(hiddenFrame) 안에서 일어납니다. 서버가
    Content-Disposition 을 안 보내면 브라우저는 다운로드가 아니라
    '표시' 를 하고, 다운로드 이벤트는 오지 않습니다. 그러면 클릭은
    성공했는데 받은 파일은 없는 상태가 됩니다.
  - 받기 전에 파일 이름을 알 수 있어 이미 받은 것을 건너뛸 수 있습니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin

# 함수 이름 -> 서버 엔드포인트. 각 JS 파일 원문에서 그대로 옮겼습니다.
ENDPOINTS: dict[str, str] = {
    "fileDownload": "fileDownServlet",
    "fileDown": "fileDown",
}

# 'fileDownload' 안에는 'fileDown' 이 들어 있습니다. 그래도 짧은 쪽으로
# 잘못 읽히지 않는 것은 **이름 뒤에 '(' 를 요구하기 때문**입니다 —
# 'fileDownload(' 에서 'fileDown' 다음 글자는 'l' 이라 그 가지가 실패하고
# 정규식이 되짚어 긴 쪽을 고릅니다. 순서는 상관없습니다(둘 다 확인함).
# '(' 요구를 빼면 그때부터 엔드포인트를 틀리게 고릅니다.
_CALL = re.compile(
    r"""\b(?P<fn>fileDownload|fileDown)\s*\(\s*
        (['"])(?P<r>.*?)\2 \s*,\s*
        (['"])(?P<s>.*?)\4 \s*,\s*
        (['"])(?P<p>.*?)\6 \s*\)""",
    re.X | re.S,
)


@dataclass(frozen=True)
class Call:
    """onclick 에서 읽어 낸 다운로드 호출."""

    fn: str                      # "fileDown" 또는 "fileDownload"
    r_file: str                  # 보여 줄 이름(인코딩된 채)
    s_file: str                  # 서버에 저장된 이름
    path: str                    # 경로(fileDown=파일, fileDownload=디렉터리)

    @property
    def endpoint(self) -> str:
        return ENDPOINTS[self.fn]

    @property
    def args(self) -> tuple[str, str, str]:
        return (self.r_file, self.s_file, self.path)


def parse(onclick: str) -> Call | None:
    """onclick 속성에서 다운로드 호출을 꺼냅니다. 없으면 None."""
    m = _CALL.search(onclick or "")
    if not m:
        return None
    return Call(m.group("fn"), m.group("r"), m.group("s"), m.group("p"))


def decode(value: str) -> str:
    """'*' 를 쓰는 퍼센트 인코딩을 사람이 읽는 문자열로 돌립니다."""
    if not value:
        return ""
    return unquote(value.replace("*", "%"), encoding="utf-8", errors="replace")


def url(page_url: str, call: Call) -> str:
    """사이트의 JS 와 똑같은 주소를 만듭니다."""
    rel = (
        f"{call.endpoint}?rFileName={call.r_file}"
        f"&sFileName={call.s_file}&filePath={call.path}"
    )
    return urljoin(page_url, rel)


def filename(call: Call) -> str:
    """저장할 이름. rFileName 이 비면 저장명, 그것도 비면 경로의 끝."""
    for candidate in (call.r_file, call.s_file):
        name = decode(candidate).strip()
        if name:
            return name
    return decode(call.path).rstrip("/").rsplit("/", 1)[-1].strip()
