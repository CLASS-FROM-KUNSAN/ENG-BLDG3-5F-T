"""탐색 모드: 실제 페이지 구조를 떠서 파일로 남깁니다.

학교가 LMS 를 개편하면 수집기가 조용히 빈손으로 돌아옵니다.
그때 이 명령을 돌려 나온 결과를 보고 urls.py 의 menuId 와
crawler.py 의 선택자를 고치면 됩니다.

개인정보 주의:
  덤프에는 학번, 이름, 교수 이메일, 쪽지 개수 같은 것이 들어갑니다.
  기본적으로 가려서 저장하지만(--no-redact 로 끌 수 있음),
  그래도 저장소에 커밋하지 마세요. 출력 폴더는 .gitignore 에 있습니다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get as get_logger
from .browser import EclassBrowser
from .crawler import _eval_json
from .urls import MENU_LINK_TEXTS, Site, parse_course_option

log = get_logger("eclass.probe")

# 가릴 패턴: 학번(7~10자리 숫자), 이메일
_REDACT = [
    (re.compile(r"\b\d{7,10}\b"), "<NUMBER>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return text


def _write(path: Path, text: str, do_redact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redact(text) if do_redact else text, encoding="utf-8")
    log.info("저장: %s", path)


def _menu_inventory(page) -> list[dict[str, str]]:
    """현재 페이지의 모든 .do 링크를 텍스트와 함께 모읍니다.

    객체 배열을 evaluate 로 바로 받으면 이 playwright(1.61)에서 None 이 와서
    'NoneType' object is not iterable 로 탐색이 멈췄습니다(강의실 PC 와 이 PC 의
    03-*-links.json 이 null). 수집기처럼 JSON 문자열로만 주고받습니다.
    """
    return _eval_json(
        page,
        """() => JSON.stringify(Array.from(document.querySelectorAll('a[href*=".do"]')).map(a => ({
              text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim(),
              href: a.href,
              onclick: a.getAttribute('onclick') || ''
           })).filter(x => x.text && x.text.length < 40))"""
    ) or []


def _form_inventory(page) -> list[dict[str, Any]]:
    """숨은 form 의 필드 이름을 모읍니다. 이게 POST 역설계의 핵심 단서입니다."""
    return _eval_json(
        page,
        """() => JSON.stringify(Array.from(document.querySelectorAll('form')).map(f => ({
              name: f.getAttribute('name') || '',
              id: f.id || '',
              action: f.getAttribute('action') || '',
              method: (f.getAttribute('method') || 'get').toLowerCase(),
              fields: Array.from(f.elements).map(e => ({
                  name: e.name || '', type: e.type || '', tag: e.tagName.toLowerCase()
              })).filter(e => e.name)
           })))"""
    ) or []


def _remember_catalog(state_dir: Path, term: str, courses: list[dict]) -> None:
    """과목 목록을 시간표 편집기용으로 남김(크롤러와 같은 형식·같은 이름 정리).
    실패해도 탐색은 계속합니다."""
    from types import SimpleNamespace

    from . import catalog
    from .crawler import _clean

    try:
        path = catalog.save(state_dir, term, [
            SimpleNamespace(course_id=c["course_id"], name=_clean(c["name"]) or c["course_id"],
                            open_type=c.get("open_type", "")) for c in courses])
    except Exception as exc:  # noqa: BLE001
        log.warning("과목 목록을 남기지 못했습니다(탐색은 계속): %s", exc)
        return
    # 시간표 편집기가 이 줄로 '목록을 받았다' 를 압니다. 문구를 바꾸면 gui 도 같이.
    print(f"과목 목록 {len(courses)}개를 남겼습니다: {path}", flush=True)


def run(    site: Site,
    username: str,
    out_dir: Path,
    *,
    headless: bool = True,
    timeout_ms: int = 30000,
    request_delay_sec: float = 1.0,
    state_path: Path | None = None,
    course_limit: int = 1,
    do_redact: bool = True,
    include_risky: bool = False,
    catalog_dir: Path | None = None,
    term: str = "",
) -> Path:
    """탐색을 수행하고 결과 폴더 경로를 돌려줍니다."""
    out_dir = Path(out_dir) / f"probe-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "probed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base_url": site.base_url,
        "pages": [],
        "courses": [],
        "menus_found": {},
        "notes": [],
    }

    with EclassBrowser(
        site, username,
        headless=headless, timeout_ms=timeout_ms,
        request_delay_sec=request_delay_sec, state_path=state_path,
    ) as br:
        # --- 1. 로그인 전 화면 ---
        br.goto(site.home)
        _write(out_dir / "01-home-before-login.html", br.page.content(), do_redact)
        _write(
            out_dir / "01-home-forms.json",
            json.dumps(_form_inventory(br.page), ensure_ascii=False, indent=2),
            do_redact,
        )

        # --- 2. 로그인 ---
        br.login()
        _write(out_dir / "02-home-after-login.html", br.page.content(), do_redact)

        anchors = _menu_inventory(br.page)
        _write(out_dir / "02-home-links.json", json.dumps(anchors, ensure_ascii=False, indent=2), do_redact)

        # --- 3. 내 강의실 목록 ---
        options = _eval_json(
            br.page,
            """() => JSON.stringify(Array.from(
                   document.querySelectorAll("select[name='courseDTO.courseId'] option")
               ).map(o => ({value: o.value, text: o.textContent.trim()})))"""
        ) or []
        courses = []
        for opt in options:
            parsed = parse_course_option(opt.get("value", ""))
            if parsed:
                courses.append(
                    {"course_id": parsed[0], "professor": parsed[1],
                     "open_type": parsed[2], "name": opt.get("text", "")}
                )
        report["courses"] = courses
        log.info("과목 %d개를 찾았습니다.", len(courses))
        if catalog_dir is not None and courses:
            _remember_catalog(catalog_dir, term, courses)
        if not courses:
            report["notes"].append(
                "과목 목록을 찾지 못했습니다. select[name='courseDTO.courseId'] 선택자가 바뀌었을 수 있습니다."
            )

        # --- 4. 과목 하나를 골라 메뉴를 전부 돌아봅니다 ---
        risky = {"exam", "survey", "discussion"}
        for course in courses[: max(1, course_limit)]:
            cid = course["course_id"]
            slug = re.sub(r"\W+", "_", course["name"])[:40] or cid
            br.goto(site.course_home(cid))
            _write(out_dir / f"03-{slug}-home.html", br.page.content(), do_redact)

            links = _menu_inventory(br.page)
            _write(out_dir / f"03-{slug}-links.json",
                   json.dumps(links, ensure_ascii=False, indent=2), do_redact)

            found: dict[str, str] = {}
            for key, texts in MENU_LINK_TEXTS.items():
                if key in risky and not include_risky:
                    continue
                for link in links:
                    if link["text"] in texts or link["text"].replace(" ", "") in {
                        t.replace(" ", "") for t in texts
                    }:
                        found[key] = link["href"]
                        break
            report["menus_found"][cid] = found

            for key, url in found.items():
                if key in risky and not include_risky:
                    continue
                try:
                    br.goto(url)
                    _write(out_dir / f"04-{slug}-{key}.html", br.page.content(), do_redact)
                    _write(out_dir / f"04-{slug}-{key}-forms.json",
                           json.dumps(_form_inventory(br.page), ensure_ascii=False, indent=2), do_redact)
                    report["pages"].append({"course": cid, "menu": key, "url": url})
                except Exception as exc:
                    log.warning("%s / %s 열기 실패: %s", slug, key, exc)
                    report["notes"].append(f"{slug}/{key} 열기 실패: {exc}")

        br.save_state()

    _write(out_dir / "report.json", json.dumps(report, ensure_ascii=False, indent=2), do_redact)

    print()
    print("=" * 62)
    print(f"탐색 결과: {out_dir}")
    print(f"  과목 {len(report['courses'])}개, 페이지 덤프 {len(report['pages'])}건")
    if report["notes"]:
        print("  확인 필요:")
        for note in report["notes"]:
            print(f"    - {note}")
    print("=" * 62)
    print("이 폴더는 .gitignore 에 있으니 커밋되지 않습니다.")
    print("구조가 바뀌어 수집이 안 될 때 이 결과를 보고 urls.py 를 고치세요.")
    return out_dir
