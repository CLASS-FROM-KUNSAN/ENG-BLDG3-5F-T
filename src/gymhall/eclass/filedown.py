"""eClass 파일 다운로드 링크(fileDown) 해석.

사이트의 /lmsdata/js/FileDown.js 원문은 이게 전부입니다:

    function fileDown(rfileName, sfileName, filePath) {
        var loc = "fileDown?rFileName=" + rfileName
                + "&sFileName=" + sfileName
                + "&filePath=" + filePath;
        hiddenFrame.location.href = loc;
    }

세 인자는 이미 서버가 알아보는 형태로 들어 있습니다. 퍼센트 인코딩인데
'%' 자리에 '*' 를 쓴 것뿐입니다(`*EC*9E*A5` = `%EC%9E%A5` = '장').
그래서 주소를 만들 때는 **그대로** 붙이면 되고, 사람이 읽을 파일 이름을
얻을 때만 '*' 를 '%' 로 바꿔 디코딩하면 됩니다.

클릭 대신 주소를 직접 만드는 이유:
  - 다운로드가 숨은 iframe(hiddenFrame) 안에서 일어납니다. 서버가
    Content-Disposition 을 안 보내면 브라우저는 그냥 그 프레임에
    문서를 '표시' 하고, 다운로드 이벤트는 오지 않습니다. 그러면
    클릭은 성공했는데 받은 파일은 없는 상태가 됩니다.
  - 받기 전에 파일 이름을 알 수 있어 이미 받은 것을 건너뛸 수 있습니다.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin

# onclick="fileDown('a','b','c');" — 따옴표 종류와 공백은 느슨하게 봅니다.
_CALL = re.compile(
    r"""fileDown\s*\(\s*
        (['"])(?P<r>.*?)\1 \s*,\s*
        (['"])(?P<s>.*?)\3 \s*,\s*
        (['"])(?P<p>.*?)\5 \s*\)""",
    re.X | re.S,
)


def parse(onclick: str) -> tuple[str, str, str] | None:
    """onclick 속성에서 (rFileName, sFileName, filePath) 를 꺼냅니다."""
    m = _CALL.search(onclick or "")
    if not m:
        return None
    return m.group("r"), m.group("s"), m.group("p")


def decode(value: str) -> str:
    """'*' 를 쓰는 퍼센트 인코딩을 사람이 읽는 문자열로 돌립니다."""
    if not value:
        return ""
    return unquote(value.replace("*", "%"), encoding="utf-8", errors="replace")


def url(page_url: str, args: tuple[str, str, str]) -> str:
    """FileDown.js 와 똑같은 주소를 만듭니다.

    인자는 서버가 기대하는 형태 그대로 두고 손대지 않습니다. 다시
    인코딩하면 '*' 가 '%2A' 가 되어 서버가 다른 파일을 찾습니다.
    """
    r, s, p = args
    rel = f"fileDown?rFileName={r}&sFileName={s}&filePath={p}"
    return urljoin(page_url, rel)


def filename(args: tuple[str, str, str]) -> str:
    """저장할 이름. rFileName 이 비면 경로의 마지막 조각을 씁니다."""
    r, _s, p = args
    name = decode(r).strip()
    if not name:
        name = decode(p).rstrip("/").rsplit("/", 1)[-1].strip()
    return name
