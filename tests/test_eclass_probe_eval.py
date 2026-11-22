"""eclass probe 가 evaluate 로 객체를 바로 받지 않는지.

강의실 PC(와 이 PC)의 실제 eclass 페이지에서 객체 배열을 evaluate 로 받으면 None 이
와서 'NoneType' object is not iterable 로 탐색이 멈췄습니다(03-*-links.json = null).
페이지 스크립트가 playwright 가 쓰는 내장 객체를 바꿔 객체 결과가 사라지는 것으로,
수집기(crawler._eval_json)처럼 JSON 문자열로만 주고받으면 됩니다.
"""

import ast
import json
from pathlib import Path

from syncnote.eclass import probe


class LossyPage:
    """실제 eclass 페이지 흉내: 객체 결과는 None, 문자열만 제대로 옴."""

    def __init__(self, links, forms, options):
        self.data = {"a[href": links, "'form'": forms, "courseDTO": options}

    def evaluate(self, expression, *args):
        if "JSON.stringify" not in expression:
            return None
        for key, value in self.data.items():
            if key in expression:
                return json.dumps(value, ensure_ascii=False)
        return "[]"


def test_probe_helpers_survive_a_page_that_loses_objects():
    page = LossyPage([{"text": "공지사항", "href": "u", "onclick": ""}],
                     [{"name": "f", "id": "", "action": "/s.do", "method": "post",
                       "fields": [{"name": "courseId", "type": "text", "tag": "input"}]}], [])
    assert [x["text"] for x in probe._menu_inventory(page)] == ["공지사항"]
    assert probe._form_inventory(page)[0]["fields"][0]["name"] == "courseId"


def test_probe_never_takes_objects_straight_from_evaluate():
    tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
    raw = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "evaluate"]
    assert raw == [], f"probe.py 에 page.evaluate 를 바로 부르는 곳: 줄 {raw} — _eval_json 으로"
