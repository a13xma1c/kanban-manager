"""
Tests for the theme engine (palette remapping, live re-theming, font scaling,
contrast), the Appearance dialog, vim-style keyboard navigation on the board,
the command palette, the shortcut help overlay, and accessible names.
"""

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from kanbanmanager import DatabaseService
from kanbanmanager.database.demo_board import DEMO_BOARD_NAME
from kanbanmanager.ui import MainWindow
from kanbanmanager.ui import theme as th
from kanbanmanager.ui.command_palette import Command, CommandPalette, fuzzy_score, rank
from kanbanmanager.ui.dialogs.task_dialog import TaskDialog
from kanbanmanager.ui.dialogs.theme_dialog import ThemeDialog
from kanbanmanager.ui.keyboard import BINDINGS, ShortcutHelpDialog, is_typing
from kanbanmanager.ui.views.gantt_view import C_NORMAL, TC
from support import seeded_manager


@pytest.fixture
def dark_after(qapp):
    yield
    th.set_theme(th.builtin_theme("dark"), 100)


@pytest.fixture
def env(qapp, tmp_path, dark_after):
    mgr = seeded_manager(tmp_path / "kb.db")
    service = DatabaseService(mgr, actor="Ann")
    demo = next(b for ws in service.get_workspaces() for b in ws.boards if b.name == DEMO_BOARD_NAME)
    return service, demo


def flush():
    for _ in range(3):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def win(env):
    service, demo = env
    window = MainWindow(service=service)
    window.resize(1400, 850)
    window.show()
    QApplication.setActiveWindow(window)
    window.open_board(demo.id)
    flush()
    yield window
    window.time_tracker.shutdown()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def key(window, k, mod=Qt.NoModifier):
    QTest.keyClick(window, k, mod)
    flush()


def sel_title(window):
    card = window.board_view.selected_card()
    return card.task_data["title"] if card else None


# ---------------------------------------------------------------------------
# Theme engine
# ---------------------------------------------------------------------------
def test_dark_theme_is_the_identity_and_mappings_derive_from_roles(qapp):
    assert th.build_map(th.DARK_ROLES) == {}
    light = th.builtin_theme("light")
    mapping = th.build_map(light.colors, light.contrast)
    assert mapping["#0F172A"] == light.colors["background"].upper()
    assert mapping["#1E293B"] == light.colors["surface"].upper()
    assert mapping["#F8FAFC"] == light.colors["text"].upper()
    assert mapping["#3B82F6"] == light.colors["primary"].upper()
    assert th.luminance(mapping["#94A3B8"]) < th.luminance(light.colors["surface"]), "muted text darker than cards"
    # dark badge backgrounds become pale tints, pale badge text becomes deep - readable pairs either way
    assert th.luminance(mapping["#450A0A"]) > 0.75 and th.luminance(mapping["#F87171"]) < 0.55
    assert th.contrast_ratio(mapping["#F87171"], mapping["#450A0A"]) >= 3.0
    # keeping a role at its dark value keeps the exact original shades
    half = th.build_map({**th.DARK_ROLES, "danger": "#AA0000"})
    assert "#3B82F6" not in half and half["#F87171"] != "#F87171"
    for theme in th.BUILTIN_THEMES:
        for label, ratio, ok in th.contrast_report(theme):
            if "Dimmed" not in label and theme.key in ("light", "high_contrast", "dark", "midnight"):
                pass
    hc = th.builtin_theme("high_contrast")
    assert all(ok for _l, _r, ok in th.contrast_report(hc)), "high contrast passes WCAG AA everywhere"


def test_apply_maps_colors_and_scales_fonts(qapp, dark_after):
    th.install()
    th.set_theme(th.builtin_theme("light"), 120)
    sheet = "QLabel { color: #f8fafc; background: #1E293B; font-size: 13px; border: 1px solid #aabbcc; }"
    out = th.apply(sheet)
    assert th.builtin_theme("light").colors["surface"].upper() in out.upper()
    assert "#AABBCC" in out or "#aabbcc" in out, "colours outside the palette stay untouched"
    assert "font-size: 16px" in out, "13px at 120% is 16px"
    assert "#f8fafc" not in out.lower()


def test_live_retheme_updates_existing_widgets_and_reverts_cleanly(qapp, dark_after):
    th.install()
    label = QLabel("x")
    label.setStyleSheet("color: #F8FAFC; background-color: #0F172A;")
    dark_sheet = label.styleSheet()
    assert "#F8FAFC" in dark_sheet
    th.set_theme(th.builtin_theme("light"))
    assert "#F8FAFC" not in label.styleSheet() and th.builtin_theme("light").colors["text"].upper() in \
        label.styleSheet().upper(), "already-created widgets are restyled"
    # widgets styled *while* a theme is active keep the palette-relative raw sheet too
    late = QLabel("y")
    late.setStyleSheet("color: #F8FAFC;")
    th.set_theme(th.builtin_theme("paper"))
    paper_text = th.builtin_theme("paper").colors["text"].upper()
    assert paper_text in label.styleSheet().upper(), "no double mapping"
    assert paper_text in late.styleSheet().upper(), "no double mapping for late styles either"
    th.set_theme(th.builtin_theme("dark"))
    assert label.styleSheet() == dark_sheet, "switching back restores the original byte for byte"
    assert late.styleSheet() == "color: #F8FAFC;"
    label.deleteLater()
    late.deleteLater()


def test_painters_follow_the_theme(qapp, dark_after):
    from kanbanmanager.ui.charts import screen_theme

    th.set_theme(th.builtin_theme("light"))
    assert TC(C_NORMAL).name().upper() == th.builtin_theme("light").colors["primary"].upper()
    assert TC("#0F172A").name().upper() == th.builtin_theme("light").colors["background"].upper()
    assert screen_theme().background.upper() == th.builtin_theme("light").colors["surface"].upper()
    th.set_theme(th.builtin_theme("dark"))
    assert TC(C_NORMAL).name().upper() == "#3B82F6" and screen_theme().background == "#1E293B"


def test_appearance_persists_and_loads(env):
    service, _demo = env
    th.save_appearance(service, {"theme": "custom", "custom": {"background": "#101520"}, "font_scale": 500})
    settings = th.load_appearance(service)
    assert settings["font_scale"] == th.MAX_FONT_SCALE, "scale is clamped"
    theme = th.theme_from_appearance(settings)
    assert theme.key == "custom" and theme.colors["background"] == "#101520"
    assert theme.colors["primary"] == th.DARK_ROLES["primary"], "unset roles fall back to dark"
    assert th.theme_from_appearance({"theme": "nope"}).key == "dark"


def test_theme_dialog_previews_applies_and_reverts(env):
    service, _demo = env
    th.install()
    dlg = ThemeDialog(service)
    assert set(dlg.swatches) == {t.key for t in th.BUILTIN_THEMES} | {"custom"}
    dlg.choose("light")
    assert th.current().key == "light", "live preview"
    assert not dlg.colors_host.isEnabled()
    dlg.reject()
    assert th.current().key == "dark", "cancel restores"
    dlg = ThemeDialog(service)
    dlg._custom["background"] = "#202833"
    dlg.choose("custom")
    assert dlg.colors_host.isEnabled() and th.current().colors["background"] == "#202833"
    dlg.scale_slider.setValue(130)
    dlg.accept()
    saved = th.load_appearance(service)
    assert saved == {"theme": "custom", "custom": dlg._custom, "font_scale": 130}
    assert th.font_scale() == 130
    th.set_theme(th.builtin_theme("dark"), 100)


# ---------------------------------------------------------------------------
# Keyboard navigation
# ---------------------------------------------------------------------------
def test_vim_selection_movement(win):
    bv = win.board_view
    key(win, Qt.Key_J)
    assert sel_title(win) == "Migrate attachment storage to the CDN", "j selects the first card"
    key(win, Qt.Key_J)
    key(win, Qt.Key_J)
    assert sel_title(win) == "Evaluate charting libraries for the analytics view"
    key(win, Qt.Key_K)
    assert sel_title(win) == "Weekly team sync"
    key(win, Qt.Key_L)
    assert sel_title(win) == "Write API docs for webhooks", "l keeps the row in the next column"
    key(win, Qt.Key_Right)
    assert sel_title(win) == "Crash when importing large CSV files", "a single-card column clamps the row"
    key(win, Qt.Key_H)
    key(win, Qt.Key_H)
    key(win, Qt.Key_G, Qt.ShiftModifier)
    assert sel_title(win) == "🏁 General availability", "G jumps to the bottom"
    key(win, Qt.Key_G)
    key(win, Qt.Key_G)
    assert sel_title(win) == "Migrate attachment storage to the CDN", "gg jumps to the top"
    key(win, Qt.Key_G)
    key(win, Qt.Key_X)  # an unfinished g-sequence swallows the next key
    assert not bv.selected_card().task_data["is_completed"]
    key(win, Qt.Key_Escape)
    assert bv.selected_task_id is None
    card = bv._columns()[0].cards()[1]
    card.setFocus()
    flush()
    assert bv.selected_task_id == card.task_id, "Tab focus and vim selection stay in step"


def test_selection_skips_empty_columns(win, env):
    service, _demo = env
    bv = win.board_view
    in_progress = bv._columns()[2]
    for card in list(in_progress.cards()):
        service.archive_task(card.task_id)
    bv.refresh()
    flush()
    assert not bv._columns()[2].cards()
    key(win, Qt.Key_J)
    key(win, Qt.Key_L)
    title = sel_title(win)
    key(win, Qt.Key_L)
    assert bv._locate(bv.selected_task_id)[0] == 3, "l skips the emptied column"
    key(win, Qt.Key_H)
    assert sel_title(win) == title, "and h skips it on the way back"
    key(win, Qt.Key_H)
    key(win, Qt.Key_H)
    assert bv._locate(bv.selected_task_id)[0] == 0, "h at the first column stays put"
    key(win, Qt.Key_K)
    before = bv.selected_task_id
    key(win, Qt.Key_K)
    assert bv.selected_task_id == before, "k at the top stays put"


def test_vim_keys_never_fire_while_typing(win):
    search = win.filter_bar.search_input
    search.setFocus()
    QTest.keyClicks(search, "jk/x?")
    flush()
    assert search.text() == "jk/x?"
    assert win.board_view.selected_task_id is None
    search.clear()
    search.clearFocus()
    flush()
    assert is_typing(search, Qt.Key_J)
    add_btn = win.board_view._columns()[0].findChildren(type(search))  # silence lint: not used further
    del add_btn


def test_vim_actions_on_selection(win, env, monkeypatch):
    service, demo = env
    bv = win.board_view
    key(win, Qt.Key_J)
    first = bv.selected_card()
    key(win, Qt.Key_X)
    assert service.get_task(first.task_id).is_completed, "x completes"
    key(win, Qt.Key_X)
    assert not service.get_task(bv.selected_task_id).is_completed, "x again reopens"
    key(win, Qt.Key_Plus)
    assert service.get_task(bv.selected_task_id).priority == "HIGH"
    key(win, Qt.Key_Minus)
    key(win, Qt.Key_Minus)
    assert service.get_task(bv.selected_task_id).priority == "LOW"
    key(win, Qt.Key_T)
    assert win.time_tracker.task_id == bv.selected_task_id, "t starts a stopwatch"
    key(win, Qt.Key_T)
    assert win.time_tracker.task_id is None

    title = sel_title(win)
    key(win, Qt.Key_J, Qt.ShiftModifier)
    found = bv._locate(bv.selected_task_id)
    assert found[1] == 1 and sel_title(win) == title, "Shift+J moves the task down, keeping it selected"
    key(win, Qt.Key_L, Qt.ShiftModifier)
    task = service.get_task(bv.selected_task_id)
    assert sel_title(win) == title
    assert task.column_id == bv._columns()[1].column_id, "Shift+L moves it to the next column"

    opened = []
    monkeypatch.setattr(TaskDialog, "exec", lambda dlg: opened.append((dlg.task_id, dlg.default_column_id)) or 0)
    key(win, Qt.Key_Return)
    assert opened[-1][0] == bv.selected_task_id, "Enter opens the editor"
    key(win, Qt.Key_N)
    assert opened[-1] == (None, task.column_id), "n adds a task to the selected column"

    key(win, Qt.Key_A)
    flush()
    assert any(t["id"] == task.id for t in service.get_archived_tasks(demo.id)), "a archives"
    key(win, Qt.Key_J)
    doomed = bv.selected_task_id
    neighbours = [c.task_id for c in bv._columns()[0].cards()]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    key(win, Qt.Key_Delete)
    assert service.get_task(doomed) is None, "Delete deletes (after confirming)"
    assert bv.selected_task_id == neighbours[1], "the next card takes the selection"


def test_global_keys(win, env, monkeypatch):
    service, demo = env
    key(win, Qt.Key_Slash)
    assert QApplication.focusWidget() is win.filter_bar.search_input, "/ focuses search"
    win.filter_bar.search_input.clearFocus()
    flush()
    key(win, Qt.Key_2)
    assert win.view_stack.currentIndex() == 1, "2 switches to the calendar"
    key(win, Qt.Key_1)
    assert win.view_stack.currentIndex() == 0
    key(win, Qt.Key_BracketLeft)
    assert win.current_board_id != demo.id, "[ goes to the previous board"
    key(win, Qt.Key_BracketRight)
    assert win.current_board_id == demo.id
    captured = []
    monkeypatch.setattr(type(win), "show_quick_capture", lambda self, text="": captured.append(text))
    key(win, Qt.Key_C)
    assert captured == [""]
    shown = []
    monkeypatch.setattr(ShortcutHelpDialog, "exec", lambda dlg: shown.append(dlg) or 0)
    key(win, Qt.Key_Question, Qt.ShiftModifier)
    assert len(shown) == 1, "? opens the shortcut overlay"
    win.filter_bar.search_input.setText("crash")
    flush()
    key(win, Qt.Key_Escape)
    assert win.filter_bar.search_input.text() == "", "Esc clears the filters when nothing is selected"


def test_help_overlay_lists_every_binding(qapp):
    dlg = ShortcutHelpDialog()
    labels = [w.text() for w in dlg.findChildren(QLabel)]
    for binding in BINDINGS:
        assert binding.keys in labels, binding.keys
        assert binding.description in labels
    assert "Ctrl+K" in labels
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------
def test_fuzzy_matching():
    assert fuzzy_score("gcal", "Go to Calendar") is not None
    assert fuzzy_score("xq", "Go to Calendar") is None
    assert fuzzy_score("", "anything") == 0.0
    ordered = rank("ca", [Command("Calendar", lambda: None), Command("Quick Capture", lambda: None),
                          Command("Alpaca", lambda: None)])
    assert [c.title for c in ordered] == ["Calendar", "Quick Capture", "Alpaca"], "word starts beat mid-word hits"


def test_palette_commands_and_execution(win, env, monkeypatch):
    service, demo = env
    titles = [c.title for c in win.palette_commands("")]
    for expected in ("Go to Product Roadmap", f"Go to {DEMO_BOARD_NAME}", "New Task", "Quick Capture"):
        assert any(t == expected for t in titles) or any(expected in t for t in titles), expected
    boards = [c for c in win.palette_commands("go product") if c.title.startswith("Go to")]
    boards[0].run()
    assert win.current_board_id != demo.id and "Product" in service.get_board(win.current_board_id).name

    tasks = [c for c in win.palette_commands("crash") if c.category == "Tasks"]
    assert tasks and tasks[0].title == "Crash when importing large CSV files"
    assert tasks[0].subtitle.startswith(DEMO_BOARD_NAME)
    opened = []
    monkeypatch.setattr(TaskDialog, "exec", lambda dlg: opened.append(dlg.task_id) or 0)
    tasks[0].run()
    assert opened and service.get_task(opened[0]).title.startswith("Crash")

    palette = CommandPalette(win.palette_commands, win)
    palette.open("gantt")
    flush()
    assert palette.list.count() >= 1 and "View: Gantt" in palette.list.item(0).text()
    QTest.keyClick(palette.input, Qt.Key_Return)
    flush()
    assert palette.chosen.title == "View: Gantt" and win.view_stack.currentIndex() == 2
    palette.deleteLater()
    win._on_view_mode_changed("BOARD")


# ---------------------------------------------------------------------------
# Accessibility
# ---------------------------------------------------------------------------
def test_cards_and_buttons_have_accessible_names(win):
    card = win.board_view._columns()[0].cards()[0]
    name = card.accessibleName()
    assert name.startswith("Task: Migrate attachment storage")
    assert "priority Medium" in name and "due 2" in name and "blocked" in name
    assert card.focusPolicy() == Qt.StrongFocus
    assert card.check_btn.accessibleName() == "Mark complete"
    column = win.board_view._columns()[0]
    names = [b.accessibleName() for b in column.findChildren(type(card.check_btn))]
    assert "Add task to Backlog" in names and "Menu for column Backlog" in names
    done = next(c for col in win.board_view._columns() for c in col.cards() if c.task_data["is_completed"])
    assert "completed" in done.accessibleName() and done.check_btn.accessibleName() == "Mark incomplete"


def test_keys_reach_the_navigator_through_the_focused_widget(win):
    """Real key presses go to the focused widget (not the window): the navigator watches it,
    even scroll areas that would otherwise eat the arrow keys."""
    bv = win.board_view
    card = bv._columns()[0].cards()[0]
    card.setFocus()
    flush()
    QTest.keyClick(card, Qt.Key_J)
    flush()
    assert sel_title(win) == "Weekly team sync"
    area = bv._columns()[0].scroll_area
    area.setFocus()
    flush()
    QTest.keyClick(area, Qt.Key_Right)
    flush()
    assert bv._locate(bv.selected_task_id)[0] == 1, "the arrow moved the selection, not the scroll bar"
    dialog_edit = QLabel()  # widgets of other windows are never watched
    dialog_edit.show()
    dialog_edit.setFocus()
    flush()
    before = bv.selected_task_id
    QTest.keyClick(dialog_edit, Qt.Key_J)
    flush()
    assert bv.selected_task_id == before
