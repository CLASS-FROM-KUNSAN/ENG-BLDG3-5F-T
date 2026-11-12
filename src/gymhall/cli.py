"""명령줄 진입점.

  python -m gymhall doctor                환경 점검 (제일 먼저 돌려 보세요)
  python -m gymhall devices               오디오 장치 목록
  python -m gymhall secrets set eclass    비밀번호 저장
  python -m gymhall eclass probe          페이지 구조 탐색
  python -m gymhall eclass sync           자료 수집 + 업로드
  python -m gymhall record start          수동 녹화
  python -m gymhall record schedule       시간표 대기
  python -m gymhall record watch          프로그램 감지 대기
  python -m gymhall transcribe queue      대기열 전사
  python -m gymhall upload <경로>         임의 파일 업로드
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime
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


def _uploader(cfg, *, dry_run: bool = False):
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


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    print("환경 점검")
    print("=" * 62)
    ok = True

    print(f"  Python          : {sys.version.split()[0]}")
    if sys.version_info < (3, 10):  # noqa: UP036 - pip 를 거치지 않고 실행한 경우 안내
        print("    ! Python 3.10 이상을 권장합니다.")
        ok = False

    # 설정
    try:
        cfg, layout = _load(args)
        print(f"  설정 파일        : {cfg.path}")
        print(f"  데이터 폴더      : {cfg.data_dir}")
        print(f"  기기 이름        : {cfg.machine_label}")
    except config_module.ConfigError as exc:
        print(f"  설정 파일        : 실패\n{exc}")
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
    for module, purpose, required in checks:
        try:
            __import__(module)
            print(f"    [O] {module:<16} {purpose}")
        except ImportError:
            mark = "[X]" if required else "[-]"
            print(f"    {mark} {module:<16} {purpose}  -> pip install {module.replace('_','-')}")
            if required:
                ok = False

    # ffmpeg
    print("\n  외부 도구")
    try:
        from .recorder.ffmpeg_util import find_ffmpeg, find_ffprobe

        print(f"    [O] ffmpeg           {find_ffmpeg()}")
        probe = find_ffprobe()
        print(f"    {'[O]' if probe else '[-]'} ffprobe          {probe or '없음(길이 계산만 영향)'}")
    except Exception as exc:
        print(f"    [X] ffmpeg           {exc}")
        if cfg.get("recording.enabled"):
            ok = False

    # 비밀값
    print("\n  자격증명")
    for name, where in secrets.status().items():
        mark = "[O]" if where != "없음" else "[-]"
        print(f"    {mark} {name:<16} {where}")

    # 오디오 장치
    if cfg.get("recording.audio.enabled"):
        print("\n  오디오 장치")
        try:
            from .recorder import devices as dev

            found = dev.list_devices()
            loop = [d for d in found if d.kind == "loopback"]
            mics = [d for d in found if d.kind == "microphone"]
            print(f"    루프백 {len(loop)}개 / 마이크 {len(mics)}개")
            if not loop and cfg.get("recording.audio.system_enabled"):
                print("    ! 시스템 소리를 잡을 루프백 장치가 없습니다.")
                ok = False
        except Exception as exc:
            print(f"    [X] {exc}")

    print("\n" + "=" * 62)
    print("모두 정상입니다." if ok else "위의 [X] 항목을 해결하세요.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def cmd_devices(args) -> int:
    from .recorder.devices import describe

    print(describe())
    return 0


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
        )
        stats = crawler.run(
            list(cfg.get("eclass.courses", [])),
            list(cfg.get("eclass.exclude_courses", [])),
        )

    log.info("수집 결과: %s", stats.line())
    for err in stats.errors[:10]:
        log.warning("  ! %s", err)

    if args.no_upload or not cfg.get("eclass.upload.enabled") or not cfg.get("github.enabled"):
        log.info("업로드는 생략합니다.")
        return 0

    _upload_eclass(cfg, layout, store, term, dry_run=args.dry_run)
    return 0


def _upload_eclass(cfg, layout: Layout, store: Store, term: str, *, dry_run: bool) -> None:
    from .release import iter_files, tag_for, upload_filter, upload_many

    excluded = {str(m) for m in cfg.get("eclass.upload.exclude_menus", [])}
    from .eclass.urls import MENU_LABELS

    excluded_labels = {MENU_LABELS.get(m, m) for m in excluded}

    # 이번 학기 폴더만 올립니다. layout.eclass 전체를 훑으면 지난 학기
    # 파일까지 매번 다시 해시 계산하게 됩니다(업로드는 중복 제거로 막히지만
    # 과목이 쌓이면 동기화가 눈에 띄게 느려집니다).
    from .paths import safe_name

    root = layout.eclass / safe_name(term)
    if not root.exists():
        log.info("업로드할 폴더가 없습니다: %s", root)
        return

    files = [p for p in iter_files(root) if not (set(p.parts) & excluded_labels)]
    files, dropped = upload_filter(
        files,
        allow_extensions=cfg.get("eclass.upload.allow_extensions", []),
        max_file_mb=int(cfg.get("eclass.upload.max_file_mb", 0)),
    )
    for path, why in dropped:
        # 조용히 빼면 사용자는 올라간 줄 압니다. 파일은 로컬에 남습니다.
        log.info("업로드 제외: %s (%s)", path.name, why)
    if dropped:
        log.info("업로드에서 뺀 파일 %d개 — 로컬에는 그대로 있습니다.", len(dropped))
    if not files:
        log.info("새로 올릴 파일이 없습니다.")
        return

    uploader = _uploader(cfg, dry_run=dry_run)
    tag = tag_for(
        str(cfg.get("github.tag_strategy", "monthly")),
        str(cfg.get("github.tag_prefix_eclass", "eclass")),
        fixed=str(cfg.get("github.fixed_tag", "archive")),
    )
    log.info("릴리스 '%s' 로 %d개 파일 업로드", tag, len(files))
    results = upload_many(uploader, files, tag, store=store)
    log.info("업로드 완료 %d건 (중복/실패 제외)", len(results))


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------
def _post_segment_factory(cfg, layout: Layout, store: Store, label: str, *,
                          upload: bool, dry_run: bool, language: str | None = None):
    from .release import tag_for
    from .transcribe.batch import enqueue_segment

    uploader = _uploader(cfg, dry_run=dry_run) if upload else None
    tag = tag_for(
        str(cfg.get("github.tag_strategy", "monthly")),
        str(cfg.get("github.tag_prefix_recording", "recordings")),
        fixed=str(cfg.get("github.fixed_tag", "archive")),
    )

    def on_done(segment) -> None:
        try:
            enqueue_segment(cfg, layout, segment, label, language=language)
        except Exception:
            log.exception("전사 대기열 등록 실패")

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
                   language: str | None = None):
    from .recorder.session import RecordingSession
    from .transcribe import realtime

    store = _store(layout, "recordings")
    out_dir = layout.session_dir(datetime.now(), label)
    out_dir.mkdir(parents=True, exist_ok=True)

    rt = realtime.maybe_start(cfg, out_dir, label)
    session = RecordingSession(
        cfg, layout, label,
        realtime_queue=rt.audio_queue if rt else None,
        on_segment_done=_post_segment_factory(cfg, layout, store, label,
                                              upload=upload, dry_run=dry_run,
                                              language=language),
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
        print(f"전사 대기 {queued}건.  python -m gymhall transcribe queue  로 처리하세요.")
    return result


def cmd_record_start(args) -> int:
    cfg, layout = _load(args)
    label = args.label or f"수동_{datetime.now():%H%M}"
    _run_recording(cfg, layout, label, minutes=args.minutes,
                   upload=not args.no_upload, dry_run=args.dry_run,
                   language=args.language)
    return 0


def cmd_record_schedule(args) -> int:
    from .recorder import schedule as sched

    cfg, layout = _load(args)
    slots = sched.parse(cfg.get("recording.timetable", []))
    if not slots:
        print(sched.describe(slots))
        return 1

    print(sched.describe(slots))
    if args.show:
        return 0

    pre = float(cfg.get("recording.pre_roll_minutes", 0))
    post = float(cfg.get("recording.post_roll_minutes", 0))

    while True:
        nxt = sched.next_occurrence(slots, pre_roll_min=pre, post_roll_min=post)
        if nxt is None:
            log.info("예정된 수업이 없습니다.")
            return 0
        log.info("다음 녹화: %s  %s ~ %s", nxt.slot.name,
                 nxt.start.strftime("%m-%d %H:%M"), nxt.end.strftime("%H:%M"))
        if not sched.wait_until(nxt.start):
            return 0
        minutes = max(1.0, (nxt.end - datetime.now()).total_seconds() / 60)
        _run_recording(cfg, layout, nxt.slot.name, minutes=minutes,
                       upload=not args.no_upload, dry_run=args.dry_run,
                       language=nxt.slot.language)
        if args.once:
            return 0


def cmd_record_watch(args) -> int:
    from .recorder.watcher import Watcher

    cfg, layout = _load(args)
    watcher = Watcher(
        processes=cfg.get("recording.triggers.watch_processes", []),
        window_titles=cfg.get("recording.triggers.watch_window_titles", []),
        interval_sec=float(cfg.get("recording.triggers.watch_interval_sec", 15)),
        stop_grace_sec=float(cfg.get("recording.triggers.watch_stop_grace_sec", 60)),
    )

    def on_start(hit: str) -> None:
        label = args.label or f"감지_{Path(hit).stem}_{datetime.now():%H%M}"
        _run_recording(
            cfg, layout, label, minutes=None,
            upload=not args.no_upload, dry_run=args.dry_run,
            stop_when=watcher.wait_until_gone,
            language=args.language,
        )
        if args.once:
            raise KeyboardInterrupt

    try:
        watcher.watch(on_start)
    except KeyboardInterrupt:
        log.info("감시를 종료합니다.")
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
    results = run_queue(cfg, layout, limit=args.limit, language=args.language)
    if not results:
        return 0

    print(f"\n전사 완료 {len(results)}건")
    for r in results:
        print(f"  {r.audio.name}  ({r.speed:.1f}x)")
        for out in r.outputs:
            print(f"    -> {out}")

    if not args.no_upload and cfg.get("github.enabled"):
        from .release import tag_for, upload_many

        store = _store(layout, "recordings")
        tag = tag_for(
            str(cfg.get("github.tag_strategy", "monthly")),
            str(cfg.get("github.tag_prefix_recording", "recordings")),
            fixed=str(cfg.get("github.fixed_tag", "archive")),
        )
        files = [p for r in results for p in r.outputs]
        upload_many(_uploader(cfg, dry_run=args.dry_run), files, tag,
                    prefix=f"{cfg.machine_label}_", store=store)
    return 0


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

    files = [target] if target.is_file() else list(iter_files(target))
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
    results = upload_many(_uploader(cfg, dry_run=args.dry_run), files, tag,
                          prefix=f"{cfg.machine_label}_", store=store)
    print(f"완료 {len(results)}건")
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
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gymhall",
        description="Eclass 자료 수집 + 화면/음성 녹화 + 전사 + GitHub 릴리스 업로드",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-c", "--config", help="config.toml 경로")
    parser.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("-q", "--quiet", action="store_true", help="콘솔 출력 끄기(파일 로그는 유지)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="환경 점검").set_defaults(func=cmd_doctor)
    sub.add_parser("devices", help="오디오 장치 목록").set_defaults(func=cmd_devices)

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
    p.add_argument("-m", "--minutes", type=float, help="녹화 길이(분). 생략 시 config 의 최대값")
    p.add_argument("--language", help="이 녹화의 전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_record_start)

    p = rec.add_parser("schedule", help="시간표대로 대기하다 녹화")
    p.add_argument("--show", action="store_true", help="시간표만 출력하고 종료")
    p.add_argument("--once", action="store_true", help="한 번만 녹화하고 종료")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_record_schedule)

    p = rec.add_parser("watch", help="프로그램이 뜨면 자동 녹화")
    p.add_argument("-l", "--label")
    p.add_argument("--language", help="전사 언어 (ko / en / auto / ko+en)")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_record_watch)

    # --- transcribe ---
    tr = sub.add_parser("transcribe", help="음성 전사").add_subparsers(dest="sub", required=True)

    p = tr.add_parser("queue", help="대기열 전사")
    p.add_argument("--show", action="store_true", help="대기열만 보기")
    p.add_argument("--limit", type=int, default=0, help="이번에 처리할 개수")
    p.add_argument("--language", help="대기열 전체에 적용할 언어 (ko / en / auto / ko+en)")
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_transcribe_queue)

    tr.add_parser("warmup", help="모델 미리 내려받기").set_defaults(func=cmd_transcribe_warmup)

    # --- upload ---
    p = sub.add_parser("upload", help="임의 파일/폴더를 릴리스에 올림")
    p.add_argument("path")
    p.add_argument("--tag", help="릴리스 태그 (기본: 설정의 전략대로)")
    p.add_argument("--dry-run", action="store_true")
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
