"""설정 창: 스위치 옆 켬/끔 설명 + 예시 파일이 바뀌면 바로 반영."""

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from syncnote.timetable import config_wizard as W  # noqa: E402

EX = """# =====
#  1. 시험
# =====
[demo]
# 녹화 스위치
#   true  = 켬 — 수업을 녹화합니다   [기본]
#   false = 끔 — 녹화하지 않습니다
rec = true
# 설명 없는 스위치
bare = false
"""


@pytest.fixture
def app(qt_app):
    return qt_app


def _dlg(tmp_path, text=EX):
    ex = tmp_path / "config.example.toml"
    ex.write_text(text, encoding="utf-8")
    return W.SettingsDialog(None, text, tmp_path / "config.toml", example_path=ex), ex


def _row(dlg, dotted):
    dlg.search.setText(dotted.split(".")[-1])
    return dlg.rows[dotted]


def test_switch_shows_on_and_off_with_current_state_bold(app, tmp_path):
    dlg, _ex = _dlg(tmp_path)
    row = _row(dlg, "demo.rec")
    html = row.toggle_label.text()
    assert "켬 — 수업을 녹화합니다" in html and "끔 — 녹화하지 않습니다" in html
    assert "<b>● 켬 — 수업을 녹화합니다</b>" in html and "(기본)" in html
    row.editor.setChecked(False)
    assert "<b>● 끔 — 녹화하지 않습니다</b>" in row.toggle_label.text()
    dlg.close()


def test_switch_without_description_tells_how_to_add_it(app, tmp_path):
    dlg, _ex = _dlg(tmp_path)
    text = _row(dlg, "demo.bare").toggle_label.text()
    assert "켬/끔 설명이 없습니다" in text and "true  = 켬 —" in text and "다시 읽기" in text
    dlg.close()


def test_example_change_is_offered_and_keeps_edited_values(app, tmp_path):
    dlg, ex = _dlg(tmp_path)
    _row(dlg, "demo.rec").editor.setChecked(False)             # 사용자가 고친 값
    new = EX.replace("# 설명 없는 스위치\nbare = false",
                     "# 설명 없는 스위치\n#   true  = 켬 — 새로 생긴 설명\n"
                     "#   false = 끔 — 끄면 이렇게 됩니다   [기본]\nbare = false\n"
                     "# 새 항목\nadded = 3")
    ex.write_text(new, encoding="utf-8")
    dlg._example_changed()
    assert dlg.reload_btn.isVisibleTo(dlg) and "2개 → 3개" in dlg.example_banner.text()
    dlg.reload_example()
    assert dlg.values["demo.rec"] is False                     # 고친 값 그대로
    assert dlg.values["demo.added"] == 3
    assert "새로 생긴 설명" in _row(dlg, "demo.bare").toggle_label.text()
    dlg.close()


def test_broken_example_change_loads_everything_but_the_broken_part(app, tmp_path):
    """깨진 예시도 '읽을 수 없습니다' 로 멈추지 않음 — 깨진 키만 빼고 불러오고 어디가 왜인지."""
    dlg, ex = _dlg(tmp_path)
    _row(dlg, "demo.rec").editor.setChecked(False)
    ex.write_text(EX.replace("bare = false", "bare = 켬"), encoding="utf-8")   # 값만 깨짐
    dlg._example_changed()
    text = dlg.example_banner.text()
    assert "그 부분만 빼고" in text and "bare = 켬" in text and "true/false" in text
    assert dlg.reload_btn.isVisibleTo(dlg)
    dlg.reload_example()
    assert "demo.rec" in dlg.by_dotted and "demo.bare" not in dlg.by_dotted
    assert dlg.values["demo.rec"] is False                      # 고친 값 그대로
    assert "1곳을 읽지 못해" in dlg.broken_banner.text()
    dlg.close()


def test_broken_config_toml_opens_and_saving_repairs_it(app, tmp_path):
    """깨진 config.toml 도 설정 창이 열리고, 저장하면 깨진 줄은 원문 주석으로 남기고 고침."""
    ex = tmp_path / "config.example.toml"
    ex.write_text(EX, encoding="utf-8")
    base = "[demo]\nrec = false\nbare = 켬\n"
    dlg = W.SettingsDialog(None, EX, tmp_path / "config.toml", base, example_path=ex)
    assert dlg.repaired and dlg.base_vals == {"demo.rec": False}
    assert "읽지 못했습니다" in dlg.broken_banner.text() and ".bak" in dlg.broken_banner.text()
    out = dlg.result_text()
    assert "# ⚠ 읽지 못한 줄: bare = 켬" in out and "rec = false" in out
    dlg.close()


def test_cp949_config_is_read_and_flagged():
    from syncnote import settings_schema as S

    text, note = S.decode('[a]\nname = "강의실"\n'.encode("cp949"))
    assert 'name = "강의실"' in text and "cp949" in note
    assert S.decode(b"[a]\nx = 1\n") == ("[a]\nx = 1\n", "")


# -- 값이 어긋날 때: '읽을 수 없습니다' 대신 원문 · 코드 근거 · 추측 · 교차검증 ------------
_REAL = Path(__file__).resolve().parents[1] / "config.example.toml"


def _real(tmp_path, example=None, base=None):
    text = example if example is not None else _REAL.read_text(encoding="utf-8")
    return W.SettingsDialog(None, text, tmp_path / "config.toml", base)


def test_wrong_kind_in_config_opens_and_explains_instead_of_crashing(app, tmp_path):
    ex = _REAL.read_text(encoding="utf-8")
    base = ex.replace("\nfps = 10", '\nfps = "10"', 1)          # 예전엔 여기서 TypeError 로 꺼짐
    dlg = _real(tmp_path, base=base)
    row = _row(dlg, "recording.video.fps")
    text = row.mismatch.text()
    assert not row.mismatch.isHidden()
    assert "① 원문" in text and 'fps = "10"' in text
    assert "config.py:" in text and "교차검증" in text
    assert "읽을 수 없" not in text
    assert row.editor.value() == 10                              # 숫자 칸에는 10 으로 보임
    assert dlg.changes() == {}                                   # 손대기 전에는 파일 그대로
    assert not row.fix_btn.isHidden()
    row.fix_btn.click()
    assert dlg.changes() == {"recording.video.fps": 10}


def test_key_lost_to_a_broken_example_line_still_has_a_page(app, tmp_path):
    ex = _REAL.read_text(encoding="utf-8").replace("\nfps = 10", "\nfps = 열", 1)
    dlg = _real(tmp_path, example=ex)
    assert "recording.video.fps" in dlg.ghost
    assert "recording.video.fps" in [f.dotted for f in dlg._first_fields()]
    row = _row(dlg, "recording.video.fps")
    assert "fps = 열" in row.mismatch.text()
    assert "<code>10</code> 인 것 같아요" in row.mismatch.text()


def test_matching_example_and_code_flag_nothing(app, tmp_path):
    dlg = _real(tmp_path)        # 설정 검사(빈 토큰 등)에 걸리는 것 말고, 코드와 어긋난 것은 없음
    assert [f.dotted for f in dlg.fields if dlg.report_for(f) and not dlg.issues_for(f)] == []
