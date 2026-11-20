"""명령줄 진입점.

  python -m syncnote doctor                환경 점검 (제일 먼저 돌려 보세요)
  python -m syncnote devices               오디오 장치 목록
  python -m syncnote secrets set eclass    비밀번호 저장
  python -m syncnote eclass probe          페이지 구조 탐색
  python -m syncnote eclass sync           자료 수집 + 업로드
  python -m syncnote record start          수동 녹화
  python -m syncnote record schedule       시간표 대기
  python -m syncnote timetable edit        시간표 편집기 (GUI, PySide6)
  python -m syncnote record watch          프로그램 감지 대기
  python -m syncnote audio record          소리만 녹음 (FFmpeg / SoX / arecord)
  python -m syncnote audio tools           터미널 녹음 도구 점검
  python -m syncnote stream start          실시간 송출 (Tailscale, 링크 -> 이슈)
  python -m syncnote stream schedule       시간표대로 수업마다 송출
  python -m syncnote stream link           송출 링크 미리 보기
  python -m syncnote transcribe queue      대기열 전사
  python -m syncnote upload <경로>         임의 파일 업로드
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as config_module
from . import secrets
from .logging_setup import get as get_logger
from .logging_setup import setup as setup_logging
from .paths import Layout
from .state import Store

log = get_logger("cli")


# ---------------------------------------------------------------------------
def _load(args) -> tuple[config_module.Config, Layout]:
    cfg = config_module.load(args.config)
    layout = Layout(cfg.data_dir)
    layout.ensure()
    setup_logging(
        layout.logs,
        level=args.log_level or str(cfg.get("general.log_level", "INFO")),
        retention_days=int(cfg.get("general.log_retention_days", 30)),
        console=not args.quiet,
    )
    for note in config_module.warnings_for(cfg):
        log.warning(note)
    return cfg, layout


def _store(layout: Layout, name: str) -> Store:
    return Store.open(layout.state / f"{name}.json")


def _uploader(cfg, *, dry_run: bool = False, course=None, what: str = ""):
    """어디로 올릴지 정합니다. **모르면 None 입니다.**

    네 경우가 있습니다.

        course_repos 비었음 + 과목 모름  -> github.repo   (기존 동작)
        course_repos 비었음 + 과목 있음  -> github.repo   (1과목 PC)
        course_repos 있음  + 과목 맞음   -> 그 과목의 레포
        course_repos 있음  + 과목 모름   -> None (올리지 않음)

    마지막이 중요합니다. 여기서 기본 레포로 흘리면 **A과목 녹화가
    B과목 레포로 갑니다.** 안 올라가는 쪽이 낫습니다.
    """
    from . import courses as courses_mod
    from .release import ReleaseUploader

    if course is None and courses_mod.load(cfg):
        log.warning(
            "업로드 건너뜀%s: 이 자료의 과목을 알 수 없습니다. "
            "--course 로 지정하거나 [[eclass.course_repos]] 에 매핑을 "
            "추가하세요. 파일은 로컬에 그대로 있습니다.",
            f"({what})" if what else "",
        )
        return None

    return ReleaseUploader(
        owner=str(getattr(course, "owner", "") or cfg.get("github.owner", "")),
        repo=str(getattr(course, "repo", "") or cfg.get("github.repo", "")),
        retry_count=int(cfg.get("github.retry_count", 4)),
        retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
        split_size_mb=int(cfg.get("github.split_size_mb", 1900)),
        draft=bool(cfg.get("github.draft", False)),
        prerelease=bool(cfg.get("github.prerelease", False)),
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    """환경 점검. --json 이면 같은 내용을 구조화해 한 줄 JSON 으로(시간표 편집기가 그림)."""
    import contextlib
    import io
    import json

    rep: dict = {"ok": True, "info": [], "config_error": None, "sections": [], "verdict": ""}
    if getattr(args, "json", False):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _doctor(args, rep)
        print(json.dumps(rep, ensure_ascii=False))
        return rc
    return _doctor(args, rep)


def _doctor(args, rep: dict) -> int:
    def info(label: str, value) -> None:
        rep["info"].append([label, str(value)])

    def section(title: str) -> list:
        items: list = []
        rep["sections"].append({"title": title, "items": items})
        return items

    print("환경 점검")
    print("=" * 62)
    ok = True

    print(f"  Python          : {sys.version.split()[0]}")
    info("Python", sys.version.split()[0])
    if sys.version_info < (3, 10):  # noqa: UP036 - pip 를 거치지 않고 실행한 경우 안내
        print("    ! Python 3.10 이상을 권장합니다.")
        section("Python").append({"mark": "!", "name": "Python", "detail": "3.10 이상을 권장합니다"})
        ok = False

    # 설정
    try:
        cfg, layout = _load(args)
        print(f"  설정 파일        : {cfg.path}")
        print(f"  데이터 폴더      : {cfg.data_dir}")
        print(f"  기기 이름        : {cfg.machine_label}")
        info("설정 파일", cfg.path)
        info("데이터 폴더", cfg.data_dir)
        info("기기 이름", cfg.machine_label)
    except config_module.ConfigError as exc:
        print(f"  설정 파일        : 실패\n{exc}")
        rep["config_error"] = str(exc)
        rep["ok"] = False
        rep["verdict"] = "설정 파일을 읽지 못해 점검을 멈췄습니다."
        return 1

    # 패키지
    checks = [
        ("requests", "GitHub 업로드", True),
        ("keyring", "비밀번호 저장", True),
        ("numpy", "오디오 처리", bool(cfg.get("recording.enabled"))),
        ("soundcard", "시스템 소리 캡처", bool(cfg.get("recording.audio.enabled"))),
        ("playwright", "Eclass 수집", bool(cfg.get("eclass.enabled"))),
        ("faster_whisper", "음성 전사", bool(cfg.get("transcribe.enabled"))),
        ("psutil", "프로그램 감지(선택)", bool(cfg.get("recording.triggers.watch_enabled"))),
    ]
    print("\n  파이썬 패키지")
    pkgs = section("파이썬 패키지")
    for module, purpose, required in checks:
        try:
            __import__(module)
            print(f"    [O] {module:<16} {purpose}")
            pkgs.append({"mark": "O", "name": module, "detail": purpose})
        except ImportError:
            mark = "[X]" if required else "[-]"
            fix = f"pip install {module.replace('_','-')}"
            print(f"    {mark} {module:<16} {purpose}  -> {fix}")
            pkgs.append({"mark": mark[1], "name": module, "detail": f"{purpose} — {fix}"})
            if required:
                ok = False

    # ffmpeg
    print("\n  외부 도구")
    tools = section("외부 도구")
    try:
        from .recorder.ffmpeg_util import find_ffmpeg, find_ffprobe

        print(f"    [O] ffmpeg           {find_ffmpeg()}")
        tools.append({"mark": "O", "name": "ffmpeg", "detail": str(find_ffmpeg())})
        probe = find_ffprobe()
        print(f"    {'[O]' if probe else '[-]'} ffprobe          {probe or '없음(길이 계산만 영향)'}")
        tools.append({"mark": "O" if probe else "-", "name": "ffprobe",
                      "detail": str(probe) if probe else "없음(길이 계산만 영향)"})
    except Exception as exc:
        print(f"    [X] ffmpeg           {exc}")
        tools.append({"mark": "X", "name": "ffmpeg", "detail": str(exc)})
        if cfg.get("recording.enabled"):
            ok = False

    # 비밀값
    print("\n  자격증명")
    creds = section("자격증명")
    for name, where in secrets.status().items():
        mark = "[O]" if where != "없음" else "[-]"
        print(f"    {mark} {name:<16} {where}")
        creds.append({"mark": mark[1], "name": name, "detail": where})

    # 오디오 장치
    if cfg.get("recording.audio.enabled"):
        print("\n  오디오 장치")
        audio = section("오디오 장치")
        try:
            from .recorder import devices as dev

            found = dev.list_devices()
            loop = [d for d in found if d.kind == "loopback"]
            mics = [d for d in found if d.kind == "microphone"]
            print(f"    루프백 {len(loop)}개 / 마이크 {len(mics)}개")
            audio.append({"mark": "O" if loop or mics else "-", "name": "장치",
                          "detail": f"루프백 {len(loop)}개 / 마이크 {len(mics)}개"})
            if not loop and cfg.get("recording.audio.system_enabled"):
                print("    ! 시스템 소리를 잡을 루프백 장치가 없습니다.")
                audio.append({"mark": "X", "name": "루프백",
                              "detail": "시스템 소리를 잡을 루프백 장치가 없습니다"})
                ok = False
        except Exception as exc:
            print(f"    [X] {exc}")
            audio.append({"mark": "X", "name": "장치", "detail": str(exc)})

    print("\n" + "=" * 62)
    verdict = "모두 정상입니다." if ok else "위의 [X] 항목을 해결하세요."
    print(verdict)
    rep["ok"] = ok
    rep["verdict"] = verdict
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def cmd_devices(args) -> int:
    from .recorder.devices import describe
    from .recorder.monitors import enumerate_monitors

    if getattr(args, "json", False):
        print(_devices_json(enumerate_monitors, probe=getattr(args, "probe", False)))
        return 0
    print(describe())
    print()
    print("화면")
    print("=" * 40)
    mons = enumerate_monitors()
    if not mons:
        print("  (목록을 읽을 수 없습니다. Windows 가 아니거나 권한 문제입니다.)")
        print("  monitor = 0 이면 전체 바탕화면을 잡습니다.")
    else:
        for m in mons:
            print(f"  {m.describe()}")
        print()
        print("  [recording.video] monitor = <번호>  (0 = 전체 바탕화면)")
        print("  window_title 을 적어 두면 monitor 는 무시됩니다.")
    return 0


def _devices_json(enumerate_monitors, probe: bool = False) -> str:
    """오디오 장치·화면 목록을 구조화(describe() 와 같은 분류·같은 설정 키).

    probe=True 면 **마이크만** 잠깐(1초) 열어 최대 레벨을 잽니다(디스크에 안 씀,
    devices.probe_mic). 정확히 0 이면 '신호 경로 없음'(가상 장치·권한 꺼짐).
    시스템 소리(루프백)는 재생 중인 게 없으면 0 이 정상이라 재지 않습니다.
    """
    import json

    from .recorder import devices as dev

    out: dict = {"audio_error": None, "groups": [], "monitors": [], "probed": bool(probe)}
    try:
        found = dev.list_devices()
    except Exception as exc:  # noqa: BLE001 — soundcard 없음 등도 화면에 보여 줌
        out["audio_error"] = str(exc)
        found = []
    levels: dict[str, dict] = {}
    if probe and found:
        try:
            for mic in dev._sc().all_microphones(include_loopback=False):
                r = dev.probe_mic(mic, duration_sec=1.0)
                levels[str(mic.name)] = {"peak": r.peak, "dead": r.dead, "error": r.error}
        except Exception as exc:  # noqa: BLE001
            out["probe_error"] = str(exc)
    for kind, title, key in (
        ("loopback", "시스템 소리 캡처용", "recording.audio.system_device"),
        ("microphone", "마이크", "recording.audio.mic_device"),
        ("speaker", "재생 장치 (참고용)", ""),
    ):
        out["groups"].append({
            "kind": kind, "title": title, "config_key": key,
            "devices": [{"name": d.name, "default": bool(d.is_default),
                         **(levels.get(d.name, {}) if kind == "microphone" else {})}
                        for d in found if d.kind == kind]})
    try:
        out["monitors"] = [{"index": m.index, "text": m.describe(), "primary": bool(m.primary)}
                           for m in enumerate_monitors()]
    except Exception:  # noqa: BLE001
        out["monitors"] = []
    return json.dumps(out, ensure_ascii=False)


def cmd_secrets(args) -> int:
    if args.action == "set":
        secrets.set_interactive(args.name)
    elif args.action == "delete":
        secrets.delete(args.name)
    else:
        print("자격증명 상태")
        print("=" * 40)
        for name, where in secrets.status().items():
            print(f"  {name:<10} {where}")
        print("\n값 자체는 절대 표시되지 않습니다.")
    return 0


# ---------------------------------------------------------------------------
# eclass
# ---------------------------------------------------------------------------
def cmd_eclass_probe(args) -> int:
    from .eclass.probe import run as probe_run
    from .eclass.urls import Site

    cfg, layout = _load(args)
    site = Site(str(cfg.get("eclass.base_url", "https://eclass.kunsan.ac.kr")))
    probe_run(
        site,
        str(cfg.get("eclass.username", "")),
        layout.state / "probe",
        headless=not args.no_headless and bool(cfg.get("eclass.headless", True)),
        timeout_ms=int(cfg.get("eclass.timeout_ms", 30000)),
        request_delay_sec=float(cfg.get("eclass.request_delay_sec", 1.0)),
        state_path=layout.state / "eclass_session.json",
        course_limit=args.courses,
        do_redact=not args.no_redact,
        include_risky=False,
        catalog_dir=layout.state,           # 시간표 편집기가 과목코드를 채우는 데 씀
        term=_term_label(),
    )
    return 0


def _term_label(when: date | None = None) -> str:
    when = when or date.today()
    return f"{when.year}-{1 if when.month <= 7 else 2}학기"


def cmd_eclass_sync(args) -> int:
    from .eclass.browser import EclassBrowser
    from .eclass.crawler import Crawler
    from .eclass.urls import Site

    cfg, layout = _load(args)
    if not cfg.get("eclass.enabled"):
        log.info("eclass.enabled = false 입니다. 아무것도 하지 않습니다.")
        return 0

    site = Site(str(cfg.get("eclass.base_url", "https://eclass.kunsan.ac.kr")))
    store = _store(layout, "eclass")
    term = args.term or _term_label()
    log.info("Eclass 수집 시작 (%s). %s", term, store.summary())

    with EclassBrowser(
        site,
        str(cfg.get("eclass.username", "")),
        headless=not args.no_headless and bool(cfg.get("eclass.headless", True)),
        timeout_ms=int(cfg.get("eclass.timeout_ms", 30000)),
        request_delay_sec=float(cfg.get("eclass.request_delay_sec", 1.0)),
        state_path=layout.state / "eclass_session.json",
    ) as browser:
        browser.login()
        crawler = Crawler(
            browser, site, layout, store,
            term=term,
            menus=cfg.section("eclass.menus"),
            risky=cfg.section("eclass.risky"),
            menu_id_overrides=cfg.section("eclass.menu_ids"),
            message_boxes=cfg.get("eclass.message_boxes", ["RECEIVE"]),
            screenshots=bool(cfg.get("eclass.screenshots", True)),
            capture_html=bool(cfg.get("eclass.capture_html", True)),
        )
        # Actions 등에서 SYNCNOTE_ECLASS_COURSES 를 주면 config 보다 우선
        # (콤마·세미콜론·줄바꿈 구분, 띄어쓰기·음절 순서 무관 퍼지 매칭).
        from .eclass.coursematch import split_selectors
        env_courses = split_selectors(os.environ.get("SYNCNOTE_ECLASS_COURSES", ""))
        stats = crawler.run(
            env_courses or list(cfg.get("eclass.courses", [])),
            list(cfg.get("eclass.exclude_courses", [])),
        )

    log.info("수집 결과: %s", stats.line())
    for err in stats.errors[:10]:
        log.warning("  ! %s", err)
    # 숫자만 보면 성공인데 실제로는 아무것도 못 받은 경우를 짚어 줍니다.
    suspicions = stats.suspicions()
    for note in suspicions:
        log.warning("※ %s", note)
    if suspicions and os.environ.get("GITHUB_ACTIONS") == "true":
        # Actions 요약에 노란 경고로 뜹니다. 초록불만 보고 넘어가지
        # 않도록 하는 것이 목적입니다.
        for note in suspicions:
            print(f"::warning title=수집 결과가 의심스럽습니다::{note}", flush=True)

    if args.no_upload or not cfg.get("eclass.upload.enabled") or not cfg.get("github.enabled"):
        log.info("업로드는 생략합니다.")
        return 0

    return _upload_eclass(cfg, layout, store, term, dry_run=args.dry_run)


def _upload_eclass(cfg, layout: Layout, store: Store, term: str, *, dry_run: bool) -> int:
    """과목(=레포)마다 따로 올립니다. 종료 코드를 돌려줍니다.

    한 과목이 실패해도 나머지는 그대로 올라가야 합니다. 예전에는 학기
    폴더 전체를 한 레포로 한 번에 올려서, 중간에 하나가 터지면 그 뒤가
    전부 멈췄습니다.

    **0 을 아무 때나 돌려주면 안 됩니다.** check_access 의 사전 차단을
    경고로 낮췄으므로(Actions 토큰은 contents:write 가 있어도
    /repos/ 응답의 permissions.push 가 false 로 옵니다), 이제 실패를
    알아채는 자리는 여기 하나뿐입니다. 2026-11-12 실행은 업로드 0건에
    종료 코드 0, CI 초록불이었습니다.
    """
    import tempfile

    from . import courses as courses_mod
    from .eclass.urls import MENU_LABELS
    from .paths import safe_name
    from .release import (
        ReleaseError,
        _human,
        bundle_filename,
        group_by_top_folder,
        iter_files,
        machine_prefix,
        make_archive,
        tag_for,
        upload_filter,
        upload_many,
        write_index,
    )
    from .release import asset_name as _asset_name

    excluded_labels = {
        MENU_LABELS.get(m, m) for m in cfg.get("eclass.upload.exclude_menus", [])
    }

    # 이번 학기 폴더만 올립니다. layout.eclass 전체를 훑으면 지난 학기
    # 파일까지 매번 다시 해시 계산하게 됩니다.
    root = layout.eclass / safe_name(term)
    if not root.exists():
        log.info("업로드할 폴더가 없습니다: %s", root)
        return 0

    files = [p for p in iter_files(root) if not (set(p.parts) & excluded_labels)]
    files, dropped = upload_filter(
        files,
        allow_extensions=cfg.get("eclass.upload.allow_extensions", []),
        max_file_mb=int(cfg.get("eclass.upload.max_file_mb", 0)),
    )
    for path, why in dropped:
        log.info("업로드 제외: %s (%s)", path.name, why)
    if dropped:
        log.info("업로드에서 뺀 파일 %d개 — 로컬에는 그대로 있습니다.", len(dropped))
    if not files:
        log.info("새로 올릴 파일이 없습니다.")
        return 0

    groups = group_by_top_folder(root, files)
    if not groups:
        log.info("새로 올릴 폴더가 없습니다.")
        return 0

    tag = tag_for(
        str(cfg.get("github.tag_strategy_eclass", "daily")),
        str(cfg.get("github.tag_prefix_eclass", "eclass")),
        fixed=str(cfg.get("github.fixed_tag", "archive")),
    )
    course_list = courses_mod.load(cfg)
    # 같은 과목이 두 강의실 PC 에 배포될 수 있습니다. 이름이 같으면 뒤에
    # 올린 쪽이 앞엣것을 지웁니다(중복 제거 기록은 PC 안에만 있어 서로를
    # 못 봅니다). 기기 이름을 에셋 이름에 넣어 애초에 겹치지 않게 합니다.
    prefix = machine_prefix(cfg.machine_label)

    # 폴더 -> 과목(레포). 과목을 안 적었으면 전부 기본 레포 한 곳으로.
    plan: dict[object, dict[str, list[Path]]] = {}
    unknown: list[str] = []
    for folder, members in sorted(groups.items()):
        course = courses_mod.by_name(course_list, folder) if course_list else None
        if course_list and course is None:
            unknown.append(folder)
            continue
        plan.setdefault(course, {})[folder] = members
    for folder in unknown:
        # 담당이 아닌 과목입니다. 받아는 뒀지만 올릴 레포가 없습니다.
        log.info("업로드 건너뜀(이 PC 담당 과목이 아님): %s", folder)

    ok, failed = 0, []
    tally: dict[str, int] = {"uploaded": 0, "skipped": 0, "failed": 0,
                             "collision": 0, "missing": 0}
    for course, folders in plan.items():
        where = course.slug if course is not None else "기본 레포"
        try:
            uploader = _uploader(cfg, dry_run=dry_run, course=course)
            uploader.ensure_repo(
                create_missing=bool(cfg.get("github.create_missing_repos", False)),
                private=bool(cfg.get("github.repo_private", True)),
            )
            # 공개 저장소 차단 가드(config 로 켜고 끔). eClass 자료에는 동급생
            # 이름·화면·파일명 등 개인정보가 섞일 수 있어, 대상 레포가 public
            # 이면 올리지 않습니다. is_public()==None(모름)이면 막지 않습니다
            # (네트워크 한 번 실패로 수집을 세우지 않음). 끄려면 config 에서
            # eclass.upload.block_public = false.
            is_pub_fn = getattr(uploader, "is_public", None)
            if bool(cfg.get("eclass.upload.block_public", True)) \
                    and callable(is_pub_fn) and is_pub_fn() is True:
                skipped_n = sum(len(v) for v in folders.values())
                msg = (f"{where}: 대상 저장소가 public 이라 올리지 않았습니다. "
                       "eClass 자료에는 개인정보가 섞일 수 있습니다. 저장소를 "
                       "private 로 바꾸거나, 정말 공개하려면 config 의 "
                       "eclass.upload.block_public = false 로 끄세요.")
                log.error("%s", msg)
                failed.append(where)
                tally["skipped"] += skipped_n
                continue
            total = sum(len(v) for v in folders.values())
            log.info("[%s] 릴리스 '%s' 로 폴더 %d개 (파일 %d개)",
                     where, tag, len(folders), total)
            with tempfile.TemporaryDirectory(prefix="syncnote-zip-") as work:
                bundles: list[Path] = []
                rows: list[tuple[str, str, int, int]] = []
                for name, members in sorted(folders.items()):
                    # 과목이면 main_, 쪽지·계정 등이면 sub_ (해시 앞에 남음)
                    zip_path = Path(work) / bundle_filename(name)
                    if make_archive(root, members, zip_path) is None:
                        continue
                    size = zip_path.stat().st_size
                    log.info("  %s: 파일 %d개 -> %s", name, len(members), _human(size))
                    bundles.append(zip_path)
                    rows.append((_asset_name(zip_path.name, prefix=prefix,
                                             unique_key=zip_path.name),
                                 name, len(members), size))
                if rows:
                    # 목차도 PC 마다 따로 올라가야 서로 덮어쓰지 않습니다.
                    index = Path(work) / f"INDEX_{cfg.machine_label or 'this-pc'}.md"
                    bundles.append(write_index(index, rows, tag=tag))
                done = upload_many(uploader, bundles, tag, prefix=prefix,
                                   store=store, root=Path(work), tally=tally)
            ok += len(done)
        except ReleaseError as exc:
            # 한 과목이 막혀도 나머지는 올라가야 합니다.
            log.error("[%s] 업로드 실패 — 다른 과목은 계속합니다: %s", where, exc)
            failed.append(where)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] 업로드 중 예상 못한 오류: %s", where, exc)
            failed.append(where)

    log.info("업로드 완료 %d건 (중복/실패 제외)", ok)
    if failed:
        log.error("업로드하지 못한 레포 %d곳: %s", len(failed), ", ".join(failed))

    # 사전 점검을 없앴으면 사후 검증이 반드시 있어야 합니다.
    blocked = (tally["failed"] + tally["collision"] + tally["missing"]
               + len(failed))
    if blocked:
        detail = (f"성공 {ok}건 / 실패 {tally['failed']} / 이름충돌 "
                  f"{tally['collision']} / 파일없음 {tally['missing']} / "
                  f"레포단위실패 {len(failed)}")
        if ok == 0:
            log.error("업로드 0건 — 한 개도 못 올렸습니다. %s", detail)
            _actions_error("업로드 전부 실패", f"{detail}. 로그의 첫 오류를 보세요.")
        else:
            log.error("일부만 올라갔습니다. %s", detail)
            _actions_error("업로드 일부 실패", detail)
        return 1
    if ok == 0 and tally["skipped"]:
        log.info("새로 올릴 것이 없습니다(전부 이미 올라가 있음 %d건).",
                 tally["skipped"])
    return 0


def _actions_error(title: str, message: str) -> None:
    """GitHub Actions 요약에 빨간 줄로 띄웁니다.

    warning 은 초록불 옆에 작게 뜨고 아무도 안 봅니다. 결정적 실패는
    ::error:: 여야 리뷰어 눈에 들어옵니다.
    """
    if os.environ.get("GITHUB_ACTIONS") == "true":
        clean = str(message).replace("\n", " ")[:400]
        print(f"::error title={title}::{clean}", flush=True)


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------
def _post_segment_factory(cfg, layout: Layout, store: Store, label: str, *,
                          course=None,
                          upload: bool, dry_run: bool, language: str | None = None):
    from .release import tag_for
    from .transcribe.batch import enqueue_segment

    uploader = _uploader(cfg, dry_run=dry_run, course=course, what=label) if upload else None
    tag = tag_for(
        str(cfg.get("github.tag_strategy", "monthly")),
        str(cfg.get("github.tag_prefix_recording", "recordings")),
        fixed=str(cfg.get("github.fixed_tag", "archive")),
    )

    def on_done(segment) -> None:
        try:
            enqueue_segment(cfg, layout, segment, label, language=language,
                            course=course)
        except Exception:
            log.exception("전사 대기열 등록 실패")

        if getattr(segment, "broken", False):
            # 장치 무음으로 중단된 세그먼트입니다. 로컬에는 두고 올리지만
            # 않습니다 — 소리 없는 영상이 백업된 것처럼 보이면 안 됩니다.
            log.warning("장치 무음으로 중단된 세그먼트는 올리지 않습니다: %s",
                        segment.path.name)
            return
        if uploader is None or not segment.path.exists() or segment.path.is_dir():
            return
        try:
            from .state import sha256_file

            digest = sha256_file(segment.path)
            if store.is_uploaded(digest):
                return
            prefix = f"{cfg.machine_label}_"
            result = uploader.upload(segment.path, tag, prefix=prefix)
            store.mark_uploaded(
                digest, tag=tag,
                asset_name=result["assets"][0].get("name", "") if result["assets"] else "",
                size=segment.path.stat().st_size,
            )
            store.save()
            if cfg.get("github.delete_local_after_upload"):
                segment.path.unlink(missing_ok=True)
                log.info("업로드 후 로컬 파일 삭제: %s", segment.path.name)
        except Exception:
            log.exception("세그먼트 업로드 실패 (로컬 파일은 남아 있습니다)")

    return on_done


def _run_recording(cfg, layout: Layout, label: str, *, minutes: float | None,
                   upload: bool, dry_run: bool, stop_when=None,
                   language: str | None = None, course=None,
                   stop_on_silence: bool = False, video_not_before: float = 0.0):
    from .recorder import lock as rec_lock

    # 한 PC 에서 녹화는 한 번에 하나만. RecordSchedule 과 RecordWatch 는
    # 서로 다른 작업이라 스케줄러가 둘 사이를 막아 주지 않습니다. 둘이
    # 같이 돌면 ffmpeg 두 개가 같은 오디오 장치를 열어 양쪽 다 무음이
    # 되는데, 둘 다 '녹화 중' 으로 보입니다.
    lock_path = layout.state / "recording.lock"
    try:
        guard = rec_lock.hold(lock_path, label)
        guard.__enter__()
    except rec_lock.RecordingBusy as exc:
        log.warning("녹화를 시작하지 않습니다: %s", exc)
        return None

    try:
        return _run_recording_locked(
            cfg, layout, label, minutes=minutes, upload=upload,
            dry_run=dry_run, stop_when=stop_when, language=language,
            course=course, stop_on_silence=stop_on_silence,
            video_not_before=video_not_before,
        )
    finally:
        guard.__exit__(None, None, None)


def _run_recording_locked(cfg, layout: Layout, label: str, *, minutes: float | None,
                          upload: bool, dry_run: bool, stop_when=None,
                          language: str | None = None, course=None,
                          stop_on_silence: bool = False,
                          video_not_before: float = 0.0):
    from .recorder.session import RecordingSession
    from .transcribe import realtime

    store = _store(layout, "recordings")
    out_dir = layout.session_dir(datetime.now(), course, label)
    out_dir.mkdir(parents=True, exist_ok=True)

    rt = realtime.maybe_start(cfg, out_dir, label)
    session = RecordingSession(
        cfg, layout, label,
        course=course,
        stop_on_silence=stop_on_silence,
        video_not_before=video_not_before,
        realtime_queue=rt.audio_queue if rt else None,
        on_segment_done=_post_segment_factory(cfg, layout, store, label,
                                              upload=upload, dry_run=dry_run,
                                              language=language, course=course),
    )

    if stop_when is not None:
        import threading

        def watchdog():
            stop_when()
            session.request_stop()

        threading.Thread(target=watchdog, daemon=True).start()

    try:
        result = session.run(duration_minutes=minutes)
    finally:
        if rt:
            rt.stop()
            rt.join(timeout=30)

    from .transcribe.batch import QUEUE_FILE, Queue

    print()
    print(result.summary())
    queued = len(Queue(layout.state / QUEUE_FILE))
    if queued:
        print(f"전사 대기 {queued}건.  python -m syncnote transcribe queue  로 처리하세요.")
    return result


def _resolve_course(cfg, *, course_id: str = "", label: str = "", when=None):
    """이 녹화가 어느 과목인지. 판단은 courses.resolve 한 곳에서만 합니다."""
    from . import courses as courses_mod

    return courses_mod.resolve(
        cfg, courses_mod.load(cfg), course_id=course_id, label=label, when=when
    )


def cmd_record_start(args) -> int:
    cfg, layout = _load(args)
    label = args.label or f"수동_{datetime.now():%H%M}"
    course = _resolve_course(cfg, course_id=getattr(args, "course", "") or "",
                             label=label, when=datetime.now())
    # _run_recording 은 다른 녹화가 이미 돌고 있으면 None 을 돌려줍니다.
    # 그걸 버리고 0 을 돌려주면, 한 프레임도 안 찍고 "성공" 으로
    # 끝납니다. 사용자가 직접 누른 명령이니 더더욱 알려야 합니다.
    result = _run_recording(cfg, layout, label, minutes=args.minutes,
                            upload=not args.no_upload, dry_run=args.dry_run,
                            language=args.language, course=course)
    if result is None:
        log.error("녹화를 하나도 하지 못했습니다(다른 녹화가 돌고 있습니다).")
        return 1
    return 0


def cmd_timetable_edit(args) -> int:
    """시간표 편집기. config 전체 검증(_load)은 하지 않습니다 — 설정 다른 곳이
    틀려 있어도 시간표는 고칠 수 있어야 합니다."""
    path = (Path(args.config).expanduser().resolve() if args.config
            else config_module.default_config_path())
    try:
        from .timetable import gui
    except ImportError as exc:
        print(missing_pyside_message(exc))
        return 1
    return gui.run(path)


def missing_pyside_message(exc: BaseException) -> str:
    """PySide6 가 없을 때. '어느 파이썬에' 깔아야 하는지가 핵심입니다 —
    py 런처·가상환경이 여럿이면 다른 파이썬에 깔고도 계속 없다고 나옵니다."""
    exe = sys.executable or "python"
    return (
        "시간표 편집기에는 PySide6 가 필요한데 이 파이썬에는 설치되어 있지 않습니다.\n"
        f"  파이썬: {exe}\n"
        f'  설치:   "{exe}" -m pip install PySide6\n'
        "  또는 scripts\\setup.bat 을 다시 실행하세요(requirements.txt 에 포함).\n"
        f"  (원인: {exc})"
    )


def cmd_record_schedule(args) -> int:
    from .recorder import schedule as sched

    cfg, layout = _load(args)
    slots = sched.parse(cfg.get("recording.timetable", []))
    off = sched.holidays(cfg.get("recording.holidays", []))
    if not slots:
        print(sched.describe(slots))
        return 1

    print(sched.describe(slots, off))
    if args.show:
        return 0

    pre = float(cfg.get("recording.pre_roll_minutes", 0))
    post = float(cfg.get("recording.post_roll_minutes", 0))

    while True:
        nxt = sched.next_occurrence(slots, pre_roll_min=pre, post_roll_min=post,
                                    skip_days=off)
        if nxt is None:
            log.info("예정된 수업이 없습니다.")
            return 0
        log.info("다음 녹화: %s  %s ~ %s", nxt.slot.name,
                 nxt.start.strftime("%m-%d %H:%M"), nxt.end.strftime("%H:%M"))
        if not sched.wait_until(nxt.start):
            return 0
        minutes = max(1.0, (nxt.end - datetime.now()).total_seconds() / 60)
        # nxt.start 는 pre-roll 만큼 당겨진 시각입니다. 화면은 당기지
        # 않고 진짜 수업 시작에 맞춰 켭니다 — 그 사이 강의실 PC 앞에
        # 앉은 사람의 화면이 찍혀 올라가면 안 됩니다. 소리는 당겨서
        # 받으므로 수업이 일찍 시작해도 말은 놓치지 않습니다.
        video_at = (nxt.start + timedelta(minutes=pre)).timestamp() if pre > 0 else 0.0
        course = _resolve_course(
            cfg,
            course_id=str(getattr(nxt.slot, "course_id", "") or ""),
            label=nxt.slot.name, when=nxt.start,
        )
        # 상시 실행이라 종료 코드가 신호가 아닙니다. 로그가 유일한
        # 신호입니다 — 이게 없으면 다음 날 아침 로그를 봐도 그 수업이
        # 유실된 줄 모릅니다. '녹화 시작' 은 찍히고 '녹화 종료' 는
        # 안 찍히는 모양이라 눈에 안 띕니다.
        result = _run_recording(cfg, layout, nxt.slot.name, minutes=minutes,
                                upload=not args.no_upload, dry_run=args.dry_run,
                                language=nxt.slot.language, course=course,
                                stop_on_silence=True, video_not_before=video_at)
        if result is None:
            log.error("[%s] 녹화를 시작하지 못했습니다(다른 녹화가 돌고 있습니다). "
                      "이 수업은 녹화되지 않았습니다.", nxt.slot.name)
        if args.once:
            return 0 if result is not None else 1


class _WatchOnceDone(Exception):
    """--once 로 한 번 감지하고 끝났습니다.

    예전에는 여기서 KeyboardInterrupt 를 던졌습니다. 그러면 바깥의
    except 가 사용자의 진짜 Ctrl+C 와 구별하지 못해, '사용자 중단'
    과 '녹화 실패' 가 같은 종료 코드로 나갔습니다. watch --once 를
    스케줄 작업으로 걸면 이게 치명적입니다.
    """


def cmd_record_watch(args) -> int:
    from .recorder.watcher import Watcher

    cfg, layout = _load(args)
    watcher = Watcher(
        processes=cfg.get("recording.triggers.watch_processes", []),
        window_titles=cfg.get("recording.triggers.watch_window_titles", []),
        interval_sec=float(cfg.get("recording.triggers.watch_interval_sec", 15)),
        stop_grace_sec=float(cfg.get("recording.triggers.watch_stop_grace_sec", 60)),
    )

    # --once 는 한 번 감지하고 끝납니다. 그 한 번이 실패했으면
    # 종료 코드로 알려야 합니다 — 지금은 KeyboardInterrupt 로 빠져나가
    # '사용자 중단' 과 '녹화 실패' 가 같은 코드였습니다.
    failures: list[str] = []

    def on_start(hit: str) -> None:
        label = args.label or f"감지_{Path(hit).stem}_{datetime.now():%H%M}"
        # 감지 트리거는 과목을 모릅니다. 시간표로 되짚고, 그래도 모르면
        # None — 로컬에만 저장하고 올리지 않습니다. '첫 과목으로' 같은
        # 폴백은 두지 않습니다(운영체제 녹화가 자료구조 레포로 갑니다).
        course = _resolve_course(cfg, course_id=getattr(args, "course", "") or "",
                                 label=label, when=datetime.now())
        result = _run_recording(
            cfg, layout, label, minutes=None,
            upload=not args.no_upload, dry_run=args.dry_run,
            stop_when=watcher.wait_until_gone,
            language=args.language, course=course,
            stop_on_silence=True,
        )
        if result is None:
            log.error("'%s' 를 감지했지만 녹화를 시작하지 못했습니다"
                      "(다른 녹화가 돌고 있습니다).", hit)
            failures.append(hit)
        if args.once:
            # 진짜 Ctrl+C 와 섞으면 안 됩니다. 사용자 중단은 130,
            # 녹화 실패는 1 이어야 스케줄러가 구분합니다.
            raise _WatchOnceDone

    try:
        watcher.watch(on_start)
    except _WatchOnceDone:
        pass
    except KeyboardInterrupt:
        log.info("감시를 종료합니다.")
        raise
    if failures:
        log.error("감지했지만 녹화하지 못한 것 %d건: %s",
                  len(failures), ", ".join(failures))
        return 1
    return 0


# ---------------------------------------------------------------------------
def cmd_transcribe_queue(args) -> int:
    from .transcribe.batch import Queue, run_queue

    cfg, layout = _load(args)
    queue = Queue(layout.state / "transcribe_queue.json")
    if args.show:
        from .transcribe.engine import pick_device
        from .transcribe.perf import Profile

        device, _ = pick_device(str(cfg.get("transcribe.batch.device", "auto")))
        profile = Profile(layout.state / "perf_profile.json")
        print(queue.describe(profile,
                             model=str(cfg.get("transcribe.batch.model", "small")),
                             device=device))
        return 0
    tally: dict[str, int] = {}
    results = run_queue(cfg, layout, limit=args.limit, language=args.language,
                        tally=tally)
    # "큐가 비어서 0건" 과 "20건을 전부 실패해서 0건" 은 다릅니다.
    # 밤새 돌려 놓은 무인 작업이 조용히 성공으로 끝나면 안 됩니다.
    if tally.get("failed") or tally.get("missing"):
        log.error("전사 실패 %d건 / 파일 사라짐 %d건 (성공 %d건). "
                  "실패한 것은 대기열에 남아 있습니다.",
                  tally.get("failed", 0), tally.get("missing", 0), len(results))
        _actions_error(
            "전사 실패",
            f"시도 {tally.get('attempted', 0)}건 중 성공 {len(results)}건",
        )
        _print_transcribe_results(results)
        return 1
    if not results:
        return 0

    _print_transcribe_results(results)

    if not args.no_upload and cfg.get("github.enabled"):
        from .release import tag_for, upload_many

        store = _store(layout, "recordings")
        tag = tag_for(
            str(cfg.get("github.tag_strategy", "monthly")),
            str(cfg.get("github.tag_prefix_recording", "recordings")),
            fixed=str(cfg.get("github.fixed_tag", "archive")),
        )
        files = [p for r in results for p in r.outputs]
        uploader = _uploader(cfg, dry_run=args.dry_run, what="전사 결과")
        if uploader is None:
            log.info("전사 결과 %d개는 로컬에만 둡니다.", len(files))
        else:
            up_tally: dict[str, int] = {}
            got = upload_many(uploader, files, tag,
                              prefix=f"{cfg.machine_label}_", store=store,
                              tally=up_tally)
            blocked = (up_tally.get("failed", 0) + up_tally.get("collision", 0)
                       + up_tally.get("missing", 0))
            if blocked:
                detail = (f"성공 {len(got)}건 / 실패 {up_tally.get('failed', 0)} / "
                          f"이름충돌 {up_tally.get('collision', 0)} / "
                          f"파일없음 {up_tally.get('missing', 0)}")
                log.error("전사 결과를 올리지 못했습니다. %s", detail)
                _actions_error("전사 결과 업로드 실패", detail)
                return 1
    return 0


def _print_transcribe_results(results) -> None:
    if not results:
        return
    print(f"\n전사 완료 {len(results)}건")
    for r in results:
        print(f"  {r.audio.name}  ({r.speed:.1f}x)")
        for out in r.outputs:
            print(f"    -> {out}")


def cmd_transcribe_warmup(args) -> int:
    from .transcribe.engine import EngineSpec

    cfg, _ = _load(args)
    for key in ("batch", "realtime"):
        if not cfg.get(f"transcribe.{key}.enabled", key == "batch"):
            continue
        spec = EngineSpec(
            model=str(cfg.get(f"transcribe.{key}.model", "small")),
            device=str(cfg.get(f"transcribe.{key}.device", "auto")),
            compute_type=str(cfg.get(f"transcribe.{key}.compute_type", "int8")),
        )
        print(f"{key} 모델 내려받는 중: {spec.model} ...")
        spec.load()
        print("  완료")
    print("\n이제 인터넷이 없는 곳에서도 전사할 수 있습니다.")
    return 0


def cmd_upload(args) -> int:
    from .release import iter_files, tag_for, upload_many

    cfg, layout = _load(args)
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"경로가 없습니다: {target}")
        return 1

    is_dir = target.is_dir()
    files = [target] if not is_dir else list(iter_files(target))
    if not files:
        print("올릴 파일이 없습니다.")
        return 0

    tag = args.tag or tag_for(
        str(cfg.get("github.tag_strategy", "monthly")),
        str(cfg.get("github.tag_prefix_recording", "recordings")),
        fixed=str(cfg.get("github.fixed_tag", "archive")),
    )
    store = _store(layout, "manual_upload")
    print(f"릴리스 '{tag}' 에 {len(files)}개 파일을 올립니다.")
    # 폴더를 올릴 때 root 를 넘기지 않으면 에셋 이름이 '파일 이름' 만으로
    # 정해져서, 하위 폴더가 다른 동명 파일(courseA/자료.md, courseB/자료.md)이
    # 같은 이름이 됩니다. 실행 내 중복 검사에 걸려 두 번째가 통째로 빠집니다.
    from . import courses as courses_mod

    course_list = courses_mod.load(cfg)
    course = courses_mod.resolve(
        cfg, course_list, course_id=getattr(args, "course", "") or ""
    )
    uploader = _uploader(cfg, dry_run=args.dry_run, course=course, what=str(target))
    if uploader is None:
        print("과목을 알 수 없어 올리지 않았습니다. --course 로 지정하세요.")
        print(f"고를 수 있는 과목: {', '.join(c.name for c in course_list) or '(없음)'}")
        return 1
    tally: dict[str, int] = {}
    results = upload_many(uploader, files, tag,
                          prefix=f"{cfg.machine_label}_", store=store,
                          root=target if is_dir else target.parent,
                          tally=tally)
    print(f"완료 {len(results)}건")

    # _upload_eclass 와 같은 자리입니다. 여기도 오래 0 을 돌려주고
    # 있었습니다 — 전부 실패해도 "완료 0건" 을 찍고 종료 코드 0.
    # 변이 테스트 세트가 eclass 경로만 봐서 이쪽은 안 걸렸습니다.
    blocked = (tally.get("failed", 0) + tally.get("collision", 0)
               + tally.get("missing", 0))
    if blocked:
        detail = (f"성공 {len(results)}건 / 실패 {tally.get('failed', 0)} / "
                  f"이름충돌 {tally.get('collision', 0)} / "
                  f"파일없음 {tally.get('missing', 0)}")
        print(f"올리지 못한 파일이 있습니다. {detail}")
        _actions_error("업로드 실패", detail)
        return 1
    return 0


def cmd_config_check(args) -> int:
    try:
        cfg = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(exc)
        return 1
    print(f"설정 파일 정상: {cfg.path}")
    notes = config_module.warnings_for(cfg)
    if notes:
        print("\n확인할 점:")
        for note in notes:
            print(f"  - {note}")
    print(f"\n데이터 폴더: {cfg.data_dir}")
    return 0


# ---------------------------------------------------------------------------
# backup (강의실 PC 홈 디렉터리)
# ---------------------------------------------------------------------------
def _home_uploader(cfg, *, dry_run: bool):
    """홈 백업은 과목과 무관합니다. course_repos 가 있어도 기본 레포로 갑니다."""
    from .release import ReleaseUploader

    return ReleaseUploader(
        owner=str(cfg.get("github.owner", "")),
        repo=str(cfg.get("github.repo", "")),
        retry_count=int(cfg.get("github.retry_count", 4)),
        retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
        split_size_mb=int(cfg.get("github.split_size_mb", 1900)),
        draft=bool(cfg.get("github.draft", False)),
        prerelease=bool(cfg.get("github.prerelease", False)),
        dry_run=dry_run,
    )


def _home_state_path(layout: Layout) -> Path:
    # Store(JSON) 가 아니라 SQLite 입니다. 홈 하나에 파일이 수십만 개라
    # JSON 한 덩어리로는 저장할 때마다 수백 MB 를 다시 씁니다.
    return layout.state / "home_backup.sqlite"


def _home_root(cfg) -> Path:
    raw = str(cfg.get("backup.home.root", "") or "").strip()
    return Path(raw).expanduser() if raw else Path.home()


def _check_home_root(home: Path) -> str | None:
    """홈 백업/정리 대상이 사용자 홈 안인지. 아니면 이유를 돌려줍니다.

    root 를 C:/Users 나 C:/ 로 적으면 AppData 가 '최상위' 가 아니게 되어
    다른 계정의 AppData, Program Files 까지 훑고, 정리가 지울 수 있는
    것은 다 지웁니다(검토에서 지적). 홈이거나 그 아래만 허용합니다.
    """
    if not home.is_dir():
        return f"홈 디렉터리가 없습니다: {home}"
    real = home.resolve()
    user_home = Path.home().resolve()
    if real != user_home and user_home not in real.parents:
        return (f"backup.home.root 가 사용자 홈({user_home}) 밖입니다: {real}. "
                "홈이거나 그 아래 폴더만 쓸 수 있습니다.")
    return None


def _home_identity(cfg, state) -> tuple[str, str]:
    """(사슬의 machine, 에셋 이름 접두사). 둘 다 설치 ID 를 품습니다."""
    from .release import machine_prefix

    iid = state.install_id()
    return f"{cfg.machine_label}#{iid}", f"{machine_prefix(cfg.machine_label)}{iid}_"


# 백업이 정리(보통 몇 분)를 기다리는 최대 시간.
HOME_LOCK_WAIT_SEC = 1800


def _home_lock_path(layout: Layout) -> Path:
    return layout.state / "home_backup.lock"


def cmd_backup_home(args) -> int:
    from .backup.homestate import HomeState
    from .backup.lock import exclusive
    from .backup.run import run_backup
    from .release import tag_for

    cfg, layout = _load(args)
    if not cfg.get("backup.home.enabled", False):
        log.info("홈 백업이 꺼져 있습니다 (backup.home.enabled = false).")
        return 0
    home = _home_root(cfg)
    why = _check_home_root(home)
    if why:
        log.error("홈 백업: %s", why)
        return 1

    tag = tag_for("daily", str(cfg.get("backup.home.tag_prefix", "home")))
    # 정리가 돌고 있으면 끝나기를 기다립니다(정리는 보통 몇 분).
    with exclusive(_home_lock_path(layout), wait_sec=HOME_LOCK_WAIT_SEC) as got:
        if not got:
            detail = "다른 홈 백업/정리가 30분 넘게 끝나지 않아 이번 백업을 건너뜁니다."
            log.error("홈 백업: %s", detail)
            _actions_error("홈 백업 건너뜀", detail)
            return 1
        with HomeState.open(_home_state_path(layout)) as state:
            machine, prefix = _home_identity(cfg, state)
            result = run_backup(
                home=home,
                uploader=_home_uploader(cfg, dry_run=args.dry_run),
                state=state,
                layout=layout,
                machine=machine,
                prefix=prefix,
                tag=tag,
                bundle_mb=int(cfg.get("backup.home.bundle_mb", 1800)),
                exclude_names=list(cfg.get("backup.home.exclude_names", [])),
                dry_run=args.dry_run,
            )
    if args.dry_run:
        return 0
    if result.failed_bundles:
        detail = f"묶음 {result.failed_bundles}개를 올리지 못했습니다. 다음 실행에서 다시 시도합니다."
        log.error("홈 백업 일부 실패: %s", detail)
        _actions_error("홈 백업 실패", detail)
        return 1
    if result.manifest_failed:
        detail = "파일은 올렸지만 사슬(매니페스트)을 올리지 못했습니다. 정리는 일어나지 않습니다."
        log.error("홈 백업: %s", detail)
        _actions_error("홈 백업 사슬 실패", detail)
        return 1
    if not getattr(result, "ok", True):
        detail = "홈 백업이 끝까지 가지 못했습니다(연결/권한 확인 실패 등). 로그를 확인하세요."
        log.error("홈 백업: %s", detail)
        _actions_error("홈 백업 실패", detail)
        return 1
    return 0


def _prune_protect(cfg, args) -> list[Path]:
    """절대 지우면 안 되는 곳: 이 프로그램 자신, 그걸 돌리는 Python, 데이터 폴더, 설정 파일.

    Python 이 홈 안(~/anaconda3, ~/scoop, 홈 안의 venv)에 깔려 있으면 그것도
    백업 대상이라 지워질 수 있습니다. 지우면 다음 날 예약 작업이 못 돕니다.
    """
    repo_root = Path(__file__).resolve().parents[2]
    cfg_path = (Path(args.config).expanduser().resolve() if args.config
                else config_module.default_config_path())
    out = [repo_root, Path(cfg.data_dir).resolve(), cfg_path]
    for raw in {sys.prefix, sys.base_prefix, sys.exec_prefix}:
        out.append(Path(raw).resolve())
    out.append(Path(sys.executable).resolve().parent)
    return out


def cmd_backup_prune(args) -> int:
    from .backup.homestate import HomeState
    from .backup.lock import exclusive
    from .backup.prune import run_prune

    cfg, layout = _load(args)
    if not cfg.get("backup.home.prune.enabled", False) and not args.dry_run:
        log.info("홈 정리가 꺼져 있습니다 (backup.home.prune.enabled = false). "
                 "--dry-run 으로 지울 목록만 볼 수 있습니다.")
        return 0
    home = _home_root(cfg)
    why = _check_home_root(home)
    if why:
        log.error("홈 정리: %s", why)
        return 1

    # 백업이 돌고 있으면(첫 백업은 몇 시간) 오늘은 정리하지 않습니다.
    with exclusive(_home_lock_path(layout), wait_sec=0) as got:
        if not got:
            log.warning("홈 백업이 아직 돌고 있어 오늘은 정리하지 않습니다.")
            return 0
        with HomeState.open(_home_state_path(layout)) as state:
            machine, _prefix = _home_identity(cfg, state)
            result = run_prune(
                home=home,
                # 정리는 사슬을 **읽어야** 합니다. dry-run 이어도 네트워크 읽기는 합니다.
                uploader=_home_uploader(cfg, dry_run=False),
                state=state,
                layout=layout,
                machine=machine,
                min_age_hours=float(cfg.get("backup.home.prune.min_age_hours", 24)),
                protect=_prune_protect(cfg, args),
                dry_run=args.dry_run,
            )
    if not result.chain_ok:
        detail = "GitHub 의 사슬을 확인하지 못해 아무것도 지우지 않았습니다."
        log.error("홈 정리: %s", detail)
        _actions_error("홈 정리 중단", detail)
        return 1
    if result.errors:
        log.error("홈 정리 중 오류 %d건 — 로그를 확인하세요.", result.errors)
        return 1
    if getattr(result, "missing_remote", 0):
        # 지우지 않았으니 안전하지만, GitHub 쪽 묶음이 없거나 검증이 안 된다는
        # 뜻입니다. 조용히 넘기면 그 파일은 영원히 정리도 재업로드도 안 됩니다.
        detail = (f"GitHub 에서 묶음을 확인하지 못한 파일 {result.missing_remote}개를 "
                  "지우지 않았습니다. 릴리스를 확인하세요.")
        log.error("홈 정리: %s", detail)
        _actions_error("홈 정리 확인 실패", detail)
        return 1
    return 0


# ---------------------------------------------------------------------------
# audio — 터미널 녹음기 (FFmpeg / SoX / arecord)
# ---------------------------------------------------------------------------
def _terminal_plan(cfg, args):
    from .recorder import terminal

    t = cfg.section("recording.terminal")

    def pick(name, key):
        value = getattr(args, name, None)
        return t.get(key) if value is None else value

    return terminal.Plan(
        backend=str(pick("backend", "backend")),
        source=str(pick("source", "source")),
        device=str(pick("device", "device") or ""),
        seconds=float(pick("seconds", "seconds")),
        fmt=str(pick("format", "format")),
        vad=bool(args.vad or t.get("vad", False)),
        vad_threshold=str(pick("threshold", "vad_threshold")),
        vad_start_sec=float(t.get("vad_start_sec", 0.1)),
        vad_stop_sec=float(pick("stop_after", "vad_stop_sec")),
        samplerate=int(t.get("samplerate", 44100)),
        channels=int(t.get("channels", 2)),
    )


def _terminal_out_dir(cfg, layout, args) -> Path:
    from .recorder import terminal

    raw = args.out or str(cfg.get("recording.terminal.out_dir", "") or "")
    if raw:
        return Path(os.path.expandvars(raw)).expanduser()
    return terminal.default_out_dir(layout.recordings)


def cmd_audio_record(args) -> int:
    from .recorder import terminal

    cfg, layout = _load(args)
    plan = _terminal_plan(cfg, args)
    pattern = args.name or str(cfg.get("recording.terminal.filename", "record_%Y%m%d_%H%M%S"))
    saved = 0

    try:
        if args.print:
            out = _terminal_out_dir(cfg, layout, args) / terminal.output_name(pattern, plan.fmt)
            backend, cmd = terminal.build(plan, out)
            print(f"# {backend}")
            print(subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd))
            return 0

        while True:
            out = terminal.record_once(plan, _terminal_out_dir(cfg, layout, args),
                                       pattern=pattern)
            if out is not None:
                saved += 1
                print(out)
            elif not args.loop:
                return 1
            if not args.loop:
                return 0
            # 장치가 사라져 곧바로 실패하는 경우 CPU 를 태우며 돌지 않게 합니다.
            if out is None:
                time.sleep(5)
    except terminal.TerminalAudioError as exc:
        print(f"\n{exc}")
        return 2
    except KeyboardInterrupt:
        log.info("터미널 녹음을 멈췄습니다. 이번 실행에서 %d개 저장.", saved)
        return 0


def cmd_audio_tools(args) -> int:
    from .recorder import terminal

    cfg, layout = _load(args)
    found = terminal.available()
    plat = terminal.platform_key()
    print(f"터미널 녹음 도구 ({plat})")
    print("=" * 62)
    for name, path in found.items():
        if name == "arecord" and plat != "linux":
            print(f"  [-] {name:<8} 리눅스 전용")
            continue
        print(f"  {'[O]' if path else '[-]'} {name:<8} {path or '없음'}")

    plan = _terminal_plan(cfg, args)
    out = _terminal_out_dir(cfg, layout, args) / terminal.output_name(
        str(cfg.get("recording.terminal.filename", "record_%Y%m%d_%H%M%S")), plan.fmt)
    print("\n지금 설정으로 실행될 명령")
    try:
        backend, cmd = terminal.build(plan, out, found=found)
    except terminal.TerminalAudioError as exc:
        print(f"  (만들 수 없음) {exc}")
        return 1
    print(f"  [{backend}] " + (subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)))
    return 0


# ---------------------------------------------------------------------------
# stream — 실시간 오디오 송출 (Tailscale)
# ---------------------------------------------------------------------------
_STREAM_FLAGS = {
    "server": "server", "source": "source", "device": "device", "codec": "codec",
    "bitrate": "bitrate_kbps", "port": "port", "minutes": "max_duration_minutes",
    "idle_stop": "idle_stop_minutes", "mode": "ts_mode",
}


def _stream_settings(cfg, args):
    from .stream import live

    s = live.settings_from(cfg)
    for flag, attr in _STREAM_FLAGS.items():
        value = getattr(args, flag, None)
        if value is not None:
            setattr(s, attr, value)
    if getattr(args, "minutes", None):
        # 명령줄의 -m 은 '이만큼만 하고 끝' 이라는 직접 지시입니다. 설정의
        # max_duration_minutes(기본: 롤백 순환)와 달리 멈춥니다. 시간표 수업
        # 중이면 그래도 시간표가 우선입니다.
        s.on_max_duration = "stop"
    if getattr(args, "no_tailscale", False):
        s.ts_enabled = False
    if getattr(args, "no_issue", False):
        s.issue.enabled = False
    return s


def _stream_notifier(cfg, layout, s, *, dry_run: bool):
    from .release import ReleaseError, ReleaseUploader
    from .stream import notify

    if not s.issue.enabled or not s.ts_enabled:
        return None
    try:
        client = ReleaseUploader(
            owner=s.issue.owner, repo=s.issue.repo,
            retry_count=int(cfg.get("github.retry_count", 4)),
            retry_backoff=float(cfg.get("github.retry_backoff", 2.0)),
            dry_run=dry_run,
        )
    except ReleaseError as exc:
        log.warning("송출 링크를 이슈로 올리지 않습니다: %s ([stream.issue] owner/repo)", exc)
        return None
    return notify.IssueNotifier(s.issue, client=client, store=_store(layout, "stream"),
                                dry_run=dry_run)


def cmd_stream_start(args) -> int:
    from .stream import capture, live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    session = live.Session(settings=s, label=args.label or "", machine=cfg.machine_label,
                           dry_run=args.dry_run, slots=live.timetable(cfg, s))
    try:
        live.check(s)
        notifier = _stream_notifier(cfg, layout, s, dry_run=args.dry_run)
        return session.run(notifier)
    except (live.StreamError, tailnet.TailscaleError, capture.CaptureError) as exc:
        print(f"\n{exc}")
        return 2


def cmd_stream_schedule(args) -> int:
    """시간표대로 기다렸다가 수업마다 송출합니다. 수업 끝 = 송출 끝."""
    from .recorder import schedule as sched
    from .stream import capture, live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    slots = live.timetable(cfg, s)
    off = sched.holidays(cfg.get("recording.holidays", []))
    print(sched.describe(slots, off).replace("다음 녹화", "다음 송출"))
    if not slots or args.show:
        return 0 if slots else 1
    live.check(s)

    while True:
        nxt = sched.next_occurrence(slots, pre_roll_min=s.pre_roll_minutes,
                                    post_roll_min=s.post_roll_minutes, skip_days=off)
        if nxt is None:
            log.info("예정된 수업이 없습니다.")
            return 0
        log.info("다음 송출: %s  %s ~ %s", nxt.slot.name,
                 nxt.start.strftime("%m-%d %H:%M"), nxt.end.strftime("%H:%M"))
        if not sched.wait_until(nxt.start):
            return 0
        session = live.Session(settings=s, label=nxt.slot.name, machine=cfg.machine_label,
                               dry_run=args.dry_run, until=nxt.end, slots=slots)
        try:
            code = session.run(_stream_notifier(cfg, layout, s, dry_run=args.dry_run))
        except (live.StreamError, tailnet.TailscaleError, capture.CaptureError) as exc:
            # 상시 실행이라 한 수업이 실패해도 다음 수업은 기다립니다.
            log.error("[%s] 송출하지 못했습니다: %s", nxt.slot.name, exc)
            code = 2
        if args.once:
            return code
        # 같은 수업을 바로 다시 잡지 않게 끝 시각을 넘깁니다.
        sched.wait_until(nxt.end)


def cmd_stream_link(args) -> int:
    from .stream import live, tailnet

    cfg, layout = _load(args)
    s = _stream_settings(cfg, args)
    print("실시간 송출 점검")
    print("=" * 62)
    print(f"  서버       : {s.server}   소리: {s.source} ({s.capture})   {s.codec} {s.bitrate_kbps}kbps")
    if not s.ts_enabled:
        print("  Tailscale  : 꺼짐 — 127.0.0.1 에서만 열리고 이슈는 올리지 않습니다.")
        return 0
    try:
        exe = tailnet.find_exe(s.ts_exe)
        node = tailnet.status(exe)
    except tailnet.TailscaleError as exc:
        print(f"  [X] {exc}")
        return 1
    ok = node.running and node.online
    print(f"  Tailscale  : {'[O]' if ok else '[X]'} {node.backend_state}  {exe}")
    print(f"  이 기기    : {node.dns_name or '(MagicDNS 없음)'}  {node.ipv4}")
    print(f"  tailnet    : {node.tailnet}")
    print(f"  노출 방식  : {s.ts_mode}" + ("  (인터넷 공개!)" if s.ts_mode == "funnel" else ""))
    try:
        live.check(s)
        port = s.ic_port if s.server == "icecast" else s.port
        path = s.ic_mount if s.server == "icecast" else s.path
        link = tailnet.build_link(node, mode=s.ts_mode, port=port, https_port=s.ts_https_port,
                                  path=path, use_magicdns=s.ts_use_magicdns)
        if s.require_token and s.server != "icecast":
            link += "?t=<고정 토큰>" if s.token else "?t=<송출마다 새 토큰>"
        print(f"  링크       : {link}")
        if s.ts_mode != "direct":
            busy = tailnet.port_in_use(tailnet.serve_status(exe), s.ts_https_port)
            print(f"  serve 포트 : {s.ts_https_port} " + (
                "[!] 이미 사용 중 (override_existing 이 꺼져 있으면 시작하지 않습니다)" if busy else "비어 있음"))
    except (live.StreamError, tailnet.TailscaleError) as exc:
        print(f"  [X] {exc}")
        ok = False
    iss = s.issue
    if iss.enabled:
        print(f"  이슈       : {iss.owner}/{iss.repo}  labels={iss.labels}  on_change={iss.on_change}"
              f"  토큰 {'포함' if iss.include_token else '가림'}")
    else:
        print("  이슈       : 올리지 않음")
    return 0 if ok else 1


def cmd_stream_cleanup(args) -> int:
    from .stream import live, tailnet

    cfg, _ = _load(args)
    s = live.settings_from(cfg)
    if s.ts_mode == "direct":
        print("direct 모드는 정리할 tailscale serve 가 없습니다.")
        return 0
    exe = tailnet.find_exe(s.ts_exe)
    tailnet.unexpose(exe, s.ts_mode, s.ts_https_port)
    print(f"tailscale {s.ts_mode} --https={s.ts_https_port} off 를 실행했습니다.")
    return 0


# ---------------------------------------------------------------------------
# activity — 활동 기록 (PDF 폼 필드 값). 지금은 하드웨어/OS 게이트 점검만.
# ---------------------------------------------------------------------------
def cmd_activity_gate(args) -> int:
    from .activity import gate as gate_mod

    cfg, _ = _load(args)
    gate_cfg = cfg.section("activity.gate")
    print("활동 기록 — 하드웨어/OS 무결성 게이트")
    print("=" * 62)
    print(f"  전용 기기(machine_is_private): {bool(cfg.get('activity.machine_is_private'))}")
    res = gate_mod.check_gate(gate_cfg)
    for code, detail in res.details.items():
        failed = f"gate_fail:{code}" in res.failures
        print(f"  {'[X]' if failed else '[O]'} {code:<14} {detail}")
    if "gate_fail:attestation_not_implemented" in res.failures:
        print("  [X] attestation    require_attestation=true 인데 미구현 → fail-closed")
    print("\n" + "=" * 62)
    if res.ok:
        print("통과 — 이 기기에서는 봉인 저장이 허용됩니다.")
    else:
        print("실패 — 저장이 거부됩니다. 위 [X] 를 해결하고 다시 검사하세요.")
        print("  (예: TPM 2.0 기기, Secure Boot·HVCI 켜기, 관리자 아닌 창에서 실행)")
    return 0 if res.ok else 1


def cmd_activity_export(args) -> int:
    import json
    from pathlib import Path

    from .activity import gate as gate_mod
    from .activity import kms
    from .activity.store import SealedStore, paths

    cfg, _ = _load(args)
    # 읽기도 게이트 적용(대칭 차단). 실패면 --break-glass 1회로만.
    try:
        gate_mod.require_for_read(cfg.section("activity.gate"), break_glass=args.break_glass)
    except gate_mod.GateError as exc:
        print(f"\n{exc}")
        return 2
    db_path, dek_path = paths(cfg)
    if not Path(dek_path).exists() or not Path(db_path).exists():
        print("아직 저장된 활동이 없습니다.")
        return 0
    store = SealedStore.open(db_path, kms.load_or_create(dek_path))
    n = 0
    for event in store.decrypt_all():
        print(json.dumps(event, ensure_ascii=False))
        n += 1
    store.close()
    print(f"# {n}건 복호", file=sys.stderr)
    return 0


def cmd_activity_watch_acrobat(args) -> int:
    from .activity import sidecar

    cfg, _ = _load(args)
    if not cfg.get("activity.enabled") or not cfg.get("activity.adapters.acrobat_js"):
        print("activity.enabled 와 activity.adapters.acrobat_js 를 켜야 합니다.")
        return 1
    try:
        return sidecar.run(cfg, once=args.once)
    except Exception as exc:
        print(f"\n{exc}")
        return 2


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syncnote",
        description="Eclass 자료 수집 + 화면/음성 녹화 + 전사 + GitHub 릴리스 업로드",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="config.toml 경로")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("-q", "--quiet", action="store_true", help="콘솔 출력 끄기(파일 로그는 유지)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="환경 점검")
    p.add_argument("--json", action="store_true", help="결과를 JSON 한 줄로(시간표 편집기용)")
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("devices", help="오디오 장치 목록")
    p.add_argument("--json", action="store_true", help="결과를 JSON 한 줄로(시간표 편집기용)")
    p.add_argument("--probe", action="store_true",
                   help="--json 과 함께: 마이크를 1초씩 열어 소리가 들어오는지 잼(저장 안 함)")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("secrets", help="비밀번호/토큰 관리")
    p.add_argument("action", choices=["set", "delete", "status"], nargs="?", default="status")
    p.add_argument("name", nargs="?", choices=list(secrets.SLOTS), default="eclass")
    p.set_defaults(func=cmd_secrets)

    p = sub.add_parser("config", help="설정 검증")
    p.add_argument("action", choices=["check"], nargs="?", default="check")
    p.set_defaults(func=cmd_config_check)

    # --- eclass ---
    ec = sub.add_parser("eclass", help="Eclass 수집").add_subparsers(dest="sub", required=True)

    p = ec.add_parser("probe", help="페이지 구조 탐색(디버그용)")
    p.add_argument("--no-headless", action="store_true", help="브라우저 창을 띄워 눈으로 확인")
    p.add_argument("--courses", type=int, default=1, help="탐색할 과목 수 (기본 1)")
    p.add_argument("--no-redact", action="store_true", help="학번/이메일을 가리지 않음")
    p.set_defaults(func=cmd_eclass_probe)

    p = ec.add_parser("sync", help="자료 수집 후 릴리스 업로드")
    p.add_argument("--no-headless", action="store_true")
    p.add_argument("--no-upload", action="store_true", help="수집만 하고 올리지 않음")
    p.add_argument("--dry-run", action="store_true", help="업로드 흉내만 냄")
    p.add_argument("--term", help="학기 라벨 (기본: 자동)")
    p.set_defaults(func=cmd_eclass_sync)

    # --- record ---
    rec = sub.add_parser("record", help="화면/음성 녹화").add_subparsers(dest="sub", required=True)

    p = rec.add_parser("start", help="지금 바로 녹화")
    p.add_argument("-l", "--label", help="세션 이름 (폴더명에 쓰임)")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("-m", "--minutes", type=float, help="녹화 길이(분). 생략 시 config 의 최대값")
    p.add_argument("--language", help="이 녹화의 전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_start)

    p = rec.add_parser("schedule", help="시간표대로 대기하다 녹화")
    p.add_argument("--show", action="store_true", help="시간표만 출력하고 종료")
    p.add_argument("--once", action="store_true", help="한 번만 녹화하고 종료")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_schedule)

    tt = sub.add_parser("timetable", help="시간표 편집기(GUI)").add_subparsers(
        dest="sub", required=True)
    tt.add_parser("edit", help="PySide6 편집기로 [[recording.timetable]] 편집").set_defaults(
        func=cmd_timetable_edit)

    p = rec.add_parser("watch", help="프로그램이 뜨면 자동 녹화")
    p.add_argument("-l", "--label")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("--language", help="전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_record_watch)

    # --- activity (활동 기록) ---
    ac = sub.add_parser(
        "activity", help="활동 기록 — PDF 폼 필드 값 (지금은 게이트 점검만)"
    ).add_subparsers(dest="sub", required=True)
    ac.add_parser("gate", help="하드웨어/OS 무결성 게이트 점검(저장 가능 여부)").set_defaults(
        func=cmd_activity_gate)
    p = ac.add_parser("export", help="봉인된 활동 로그를 복호해 JSONL 로 출력(게이트 적용)")
    p.add_argument("--break-glass", action="store_true", dest="break_glass",
                   help="게이트 실패 상태에서도 1회 복호 허용(감사 로그에 남음)")
    p.set_defaults(func=cmd_activity_export)
    p = ac.add_parser("watch-acrobat", help="데스크톱 Acrobat 사이드카를 감시해 봉인(C 어댑터)")
    p.add_argument("--once", action="store_true", help="대기 중인 사이드카만 한 번 처리하고 종료")
    p.set_defaults(func=cmd_activity_watch_acrobat)

    # --- stream (실시간 송출) ---
    st = sub.add_parser(
        "stream", help="실시간 오디오 송출 (Tailscale 망, 링크를 GitHub 이슈로)"
    ).add_subparsers(dest="sub", required=True)

    def _stream_opts(p):
        p.add_argument("--server", choices=["builtin", "icecast"])
        p.add_argument("--source", choices=["system", "mic"])
        p.add_argument("-d", "--device", help="장치 이름 (비우면 기본 장치)")
        p.add_argument("--codec", choices=["mp3", "aac"])
        p.add_argument("--bitrate", type=int, help="kbps")
        p.add_argument("--port", type=int, help="내장 서버 포트")
        p.add_argument("--mode", choices=["direct", "serve", "funnel"],
                       help="Tailscale 노출 방식 (funnel 은 allow_funnel 필요)")
        p.add_argument("--no-tailscale", action="store_true", help="127.0.0.1 에서만 (이슈 없음)")
        p.add_argument("--no-issue", action="store_true", help="이슈를 올리지 않음")

    p = st.add_parser("start", help="지금 송출 시작 (설정은 [stream])")
    _stream_opts(p)
    p.add_argument("-l", "--label", help="이슈 제목 등에 쓰는 이름")
    p.add_argument("-m", "--minutes", type=float, help="이만큼 송출하고 멈춤")
    p.add_argument("--idle-stop", type=float, dest="idle_stop",
                   help="청취자가 이만큼(분) 없으면 멈춤")
    p.add_argument("--dry-run", action="store_true",
                   help="송출은 하되 이슈는 만들지 않고 내용만 출력")
    p.set_defaults(func=cmd_stream_start)

    p = st.add_parser("schedule", help="시간표([[recording.timetable]])대로 수업마다 송출")
    _stream_opts(p)
    p.add_argument("--show", action="store_true", help="송출할 시간표만 출력")
    p.add_argument("--once", action="store_true", help="한 수업만 송출하고 종료")
    p.add_argument("--dry-run", action="store_true", help="이슈는 만들지 않고 내용만 출력")
    p.set_defaults(func=cmd_stream_schedule)

    p = st.add_parser("link", help="Tailscale 상태와 만들어질 링크 미리 보기")
    _stream_opts(p)
    p.set_defaults(func=cmd_stream_link)

    st.add_parser("cleanup", help="비정상 종료로 남은 tailscale serve/funnel 끄기").set_defaults(
        func=cmd_stream_cleanup)

    # --- audio (터미널 녹음기) ---
    au = sub.add_parser(
        "audio", help="소리만 녹음 (FFmpeg / SoX / arecord, cron·작업 스케줄러용)"
    ).add_subparsers(dest="sub", required=True)

    def _audio_opts(p):
        p.add_argument("-b", "--backend", choices=["auto", "ffmpeg", "sox", "arecord"])
        p.add_argument("--source", choices=["mic", "system"],
                       help="mic(기본 입력) 또는 system(스피커로 나가는 소리)")
        p.add_argument("-d", "--device", help="장치 이름/번호 (비우면 기본 장치)")
        p.add_argument("-t", "--seconds", type=float,
                       help="녹음 길이(초). --vad 와 함께면 최대 길이")
        p.add_argument("-f", "--format", choices=["wav", "mp3", "flac"])
        p.add_argument("--vad", action="store_true",
                       help="소리가 날 때만 녹음 (SoX silence 효과)")
        p.add_argument("--threshold", help="소리 감지 문턱값 (예: 3%%, -40d)")
        p.add_argument("--stop-after", type=float, dest="stop_after",
                       help="이만큼(초) 조용하면 끝 (기본 3)")
        p.add_argument("-o", "--out", help="저장 폴더")

    p = au.add_parser("record", help="지금 녹음 (설정은 [recording.terminal])")
    _audio_opts(p)
    p.add_argument("-n", "--name", help="파일 이름 형식 (strftime, 예: lecture_%%Y%%m%%d_%%H%%M)")
    p.add_argument("--loop", action="store_true",
                   help="끝나면 새 파일로 다시 녹음 (--vad 와 함께 상시 감시)")
    p.add_argument("--print", action="store_true", help="실행하지 않고 명령만 출력")
    p.set_defaults(func=cmd_audio_record)

    p = au.add_parser("tools", help="설치된 도구와 실행될 명령 보기")
    _audio_opts(p)
    p.set_defaults(func=cmd_audio_tools)

    # --- transcribe ---
    tr = sub.add_parser("transcribe", help="음성 전사").add_subparsers(dest="sub", required=True)

    p = tr.add_parser("queue", help="대기열 전사")
    p.add_argument("--show", action="store_true", help="대기열만 보기")
    p.add_argument("--limit", type=int, default=0, help="이번에 처리할 개수")
    p.add_argument("--language", help="대기열 전체에 적용할 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_transcribe_queue)

    tr.add_parser("warmup", help="모델 미리 내려받기").set_defaults(func=cmd_transcribe_warmup)

    # --- backup ---
    bk = sub.add_parser("backup", help="강의실 PC 홈 디렉터리 백업/정리").add_subparsers(
        dest="sub", required=True)

    p = bk.add_parser("home", help="홈 디렉터리를 home-날짜 릴리스에 올림")
    p.add_argument("--dry-run", action="store_true", help="올릴 목록만 보여 줌")
    p.set_defaults(func=cmd_backup_home)

    p = bk.add_parser("prune", help="GitHub 사슬로 확인된 파일을 로컬에서 지움")
    p.add_argument("--dry-run", action="store_true",
                   help="사슬은 확인하되 지우지 않고 목록만 보여 줌")
    p.set_defaults(func=cmd_backup_prune)

    # --- upload ---
    p = sub.add_parser("upload", help="임의 파일/폴더를 릴리스에 올림")
    p.add_argument("path")
    p.add_argument("--tag", help="릴리스 태그 (기본: 설정의 전략대로)")
    p.add_argument("--course", default="",
                   help="과목(코드 또는 이름). [[eclass.course_repos]] 를 쓸 때 필요합니다.")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 흉내만 냄 (녹화·전사는 실제로 합니다)")
    p.set_defaults(func=cmd_upload)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n중단했습니다.")
        return 130
    except config_module.ConfigError as exc:
        print(f"\n설정 오류:\n{exc}")
        return 2
    except secrets.SecretsError as exc:
        print(f"\n{exc}")
        return 3
    except Exception as exc:
        log.error("예기치 못한 오류: %s", exc)
        if getattr(args, "log_level", "") == "DEBUG":
            traceback.print_exc()
        else:
            print(f"\n오류: {exc}")
            print("자세한 내용은 로그 폴더를 보거나 --log-level DEBUG 로 다시 실행하세요.")
        return 1
