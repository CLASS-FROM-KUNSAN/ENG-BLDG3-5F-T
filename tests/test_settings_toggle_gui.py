"""설정 창: 스위치 옆 켬/끔 설명 + 예시 파일이 바뀌면 바로 반영."""

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


def test_broken_example_change_is_reported_without_breaking_the_window(app, tmp_path):
    dlg, ex = _dlg(tmp_path)
    ex.write_text("[demo\nrec = ", encoding="utf-8")
    dlg._example_changed()
    assert "읽을 수 없습니다" in dlg.example_banner.text() and not dlg.reload_btn.isVisibleTo(dlg)
    assert "demo.rec" in dlg.values
    dlg.close()
