"""
Shared test infrastructure (loaded by pytest before any test module).

* **Isolation** - every test process gets its own throw-away data directory
  (``KANBAN_DATA_DIR``) *before* ``kanbanmanager`` is imported, so tests never
  touch ``~/.kanban_manager``; Qt runs offscreen; the acting user is fixed.
* **One QApplication per process**, created up front.  (A bare QCoreApplication
  made by a non-UI test would otherwise break every later widget test that
  happened to run in the same process - e.g. under ``pytest -n``.)
* **Automatic Qt teardown** after every test: windows and dialogs the test
  created are shut down and destroyed, pending deletes are flushed and the
  theme is reset.  Leftover windows would keep reacting to the global
  ``app_signals`` and make every later test slower.
* **Modal watchdog** - a dialog that a test forgot to stub out would block the
  run forever; instead it is closed after a few seconds and the test fails
  with the dialog's name.
* **Seeded database cache** - ``seeded_manager(path)`` copies a demo database
  built once per process instead of re-seeding it for every test.

Markers: ``ui`` (needs widgets; applied automatically to tests using the
``qapp``-based fixtures or importing widgets), ``slow`` (subprocesses, big data),
``perf`` (performance budgets and query-count guards).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import weakref
from pathlib import Path

# --- environment: must happen before kanbanmanager (config.py) is imported ----------------------
_SESSION_DIR = Path(tempfile.mkdtemp(prefix="kanban-tests-"))
os.environ["KANBAN_DATA_DIR"] = str(_SESSION_DIR / "data")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("KANBAN_USER", "Tester")

TESTS_DIR = Path(__file__).resolve().parent
SRC = TESTS_DIR.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import pytest  # noqa: E402
from PySide6.QtCore import QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

from support import flush_qt, seeded_manager  # noqa: E402

MODAL_TIMEOUT_S = float(os.environ.get("KANBAN_TEST_MODAL_TIMEOUT", "8"))

_app = QApplication.instance() or QApplication([])


# ---------------------------------------------------------------------------
# Pytest hooks
# ---------------------------------------------------------------------------
def pytest_configure(config):
    config.addinivalue_line("markers", "ui: needs Qt widgets (an offscreen QApplication)")
    config.addinivalue_line("markers", "slow: runs subprocesses or large data sets")
    config.addinivalue_line("markers", "perf: performance budgets and query-count guards")


def pytest_unconfigure(config):
    shutil.rmtree(_SESSION_DIR, ignore_errors=True)


def pytest_collection_modifyitems(config, items):
    ui_modules = {}
    for item in items:
        module = item.module
        if module not in ui_modules:
            names = vars(module)
            ui_modules[module] = (module.__name__.endswith("_ui") or module.__name__ == "test_ui"
                                  or any(isinstance(v, type) and issubclass(v, QWidget) for v in names.values()))
        if ui_modules[module]:
            item.add_marker(pytest.mark.ui)


# ---------------------------------------------------------------------------
# Qt application and per-test cleanup
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def qapp():
    """The process-wide QApplication (offscreen)."""
    return _app


class ModalWatchdog(QObject):
    """Closes modal dialogs / popup menus left open longer than ``timeout`` seconds."""

    def __init__(self, timeout: float):
        super().__init__()
        self.timeout = timeout
        self.closed: list = []
        self._first_seen: dict = {}
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self.closed.clear()
        self._first_seen.clear()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        for widget in (QApplication.activeModalWidget(), QApplication.activePopupWidget()):
            if widget is None:
                continue
            seen = self._first_seen.setdefault(id(widget), time.monotonic())
            if time.monotonic() - seen < self.timeout:
                continue
            title = widget.windowTitle() or type(widget).__name__
            self.closed.append(f"{type(widget).__name__} '{title}'")
            if isinstance(widget, QDialog):
                widget.reject()
            else:
                widget.close()


_watchdog = ModalWatchdog(MODAL_TIMEOUT_S)


def _shutdown_widget(widget: QWidget) -> None:
    shutdown = getattr(widget, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except RuntimeError:  # already half-destroyed
            pass
    widget.hide()
    widget.deleteLater()


_managers: "weakref.WeakSet" = weakref.WeakSet()


def _track_managers() -> None:
    """Remember every DatabaseManager so its connection pool can be closed after the test."""
    from kanbanmanager.database.connection import DatabaseManager

    if getattr(DatabaseManager, "_tracked_by_tests", False):
        return
    original = DatabaseManager.__init__

    def tracking_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        _managers.add(self)

    DatabaseManager.__init__ = tracking_init
    DatabaseManager._tracked_by_tests = True


@pytest.fixture(autouse=True)
def _qt_isolation():
    """Destroy every top-level widget a test creates, close the databases it opened and
    restore the default theme."""
    _track_managers()
    before = {id(w) for w in QApplication.topLevelWidgets()}
    app_style = _app.styleSheet()
    managers_before = set(map(id, _managers))
    _watchdog.start()
    yield
    _watchdog.stop()
    leftovers = [w for w in QApplication.topLevelWidgets() if id(w) not in before]
    for widget in leftovers:
        _shutdown_widget(widget)
    flush_qt()
    for manager in list(_managers):
        if id(manager) not in managers_before:
            manager.engine.dispose()  # close pooled SQLite connections (no ResourceWarnings)
    if _app.styleSheet() != app_style:  # e.g. the capture entry point styles the whole app
        _app.setStyleSheet(app_style)
    from kanbanmanager.ui import theme
    if theme.current().key != "dark" or theme.font_scale() != 100:
        theme.set_theme(theme.builtin_theme("dark"), 100)
    if _watchdog.closed:
        pytest.fail("the test left modal dialog(s) open (closed by the watchdog): "
                    + ", ".join(_watchdog.closed), pytrace=False)


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded_service(tmp_path):
    """A service on a fresh copy of the seeded demo database (acting user "Ann")."""
    from kanbanmanager.database.service import DatabaseService
    return DatabaseService(seeded_manager(tmp_path / "seeded.db"), actor="Ann")


@pytest.fixture
def empty_service(tmp_path):
    """A service on an empty database (no workspaces)."""
    from kanbanmanager.database.service import DatabaseService
    return DatabaseService(seeded_manager(tmp_path / "empty.db", seed=False), actor="Ann")
