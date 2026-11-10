"""
Helpers shared by the tests (import with ``from support import ...``; the
``tests`` directory is on ``sys.path`` via conftest).

* ``flush_qt()`` - process pending events and deferred deletes.
* ``seeded_manager(path)`` - a seeded demo database, copied from a per-process cache.
* ``BoardBuilder`` - compact factory for boards with columns, labels, tasks and
  dependencies (``make_board(service, tasks=200)`` for bulk data).
* ``count_queries(manager)`` - context manager counting SQL statements, for
  N+1 guards.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


def flush_qt(rounds: int = 3) -> None:
    """Process pending events and deferred deletes."""
    for _ in range(rounds):
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


# ---------------------------------------------------------------------------
# Seeded databases
# ---------------------------------------------------------------------------
def _template_dir() -> Path:
    data_dir = os.environ.get("KANBAN_DATA_DIR")
    base = Path(data_dir).parent if data_dir else Path(tempfile.gettempdir()) / f"kanban-tests-{os.getpid()}"
    return base / "seed-template"


def _template_db() -> Path:
    db = _template_dir() / "seed.db"
    if not db.exists():
        from kanbanmanager.database.connection import DatabaseManager
        db.parent.mkdir(parents=True, exist_ok=True)
        building = DatabaseManager(db.with_suffix(".building.db"))
        building.init_database(seed_defaults=True)
        building.engine.dispose()  # checkpoints the WAL into the main file
        staged = db.with_suffix(".building.db")
        attachments = staged.parent / f"{staged.stem}_attachments"
        if attachments.exists():
            attachments.rename(db.parent / f"{db.stem}_attachments")
        staged.rename(db)
    return db


def seeded_manager(path, seed: bool = True):
    """A ``DatabaseManager`` at ``path``: with ``seed`` the default workspaces and the demo
    board, exactly as ``init_database(seed_defaults=True)`` makes them (copied from a cache
    built once per process); without, an empty schema."""
    from kanbanmanager.database.connection import DatabaseManager

    path = Path(path)
    if seed:
        template = _template_db()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, path)
        attachments = template.parent / f"{template.stem}_attachments"
        if attachments.exists():
            shutil.copytree(attachments, path.parent / f"{path.stem}_attachments", dirs_exist_ok=True)
    manager = DatabaseManager(path)
    manager.init_database(seed_defaults=seed)
    return manager


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------
@dataclass
class BuiltBoard:
    board_id: int
    workspace_id: int
    columns: List[int]
    labels: List[int]
    tasks: List[int] = field(default_factory=list)

    @property
    def id(self) -> int:
        return self.board_id


def make_board(service, name: str = "Board", tasks: int = 0, labels: int = 3,
               dependencies: int = 0, with_dates: bool = False, workspace_id: Optional[int] = None,
               columns_def: Optional[List[Dict]] = None) -> BuiltBoard:
    """A board with the standard columns and ``tasks`` tasks spread round-robin over them.
    Every third task gets a label, ``dependencies`` chains task i -> i+1, and ``with_dates``
    gives tasks start/due dates (for calendar, Gantt and analytics views)."""
    ws_id = workspace_id or service.create_workspace(f"{name} workspace").id
    board = service.create_board(ws_id, name, columns_def=columns_def, labels_def=[])
    columns = [c.id for c in service.get_columns_for_board(board.id)]
    label_ids = [service.create_label(board.id, f"Label {i}", "#3B82F6").id for i in range(labels)]
    built = BuiltBoard(board.id, ws_id, columns, label_ids)
    priorities = ["LOW", "MEDIUM", "HIGH", "URGENT"]
    today = datetime.now().replace(hour=17, minute=0, second=0, microsecond=0)
    for i in range(tasks):
        kwargs = {}
        if label_ids and i % 3 == 0:
            kwargs["label_ids"] = [label_ids[i % len(label_ids)]]
        if with_dates:
            kwargs["start_date"] = (today - timedelta(days=i % 10)).date()
            kwargs["due_date"] = today + timedelta(days=(i % 14) - 3)
        task = service.create_task(board.id, columns[i % len(columns)], f"Task {i:04d}",
                                   description=f"Details for **task {i}**", priority=priorities[i % 4],
                                   estimated_hours=float(i % 8), **kwargs)
        built.tasks.append(task.id)
    for i in range(min(dependencies, max(0, tasks - 1))):
        service.add_dependency(built.tasks[i + 1], built.tasks[i])
    return built


# ---------------------------------------------------------------------------
# Query counting (N+1 guards)
# ---------------------------------------------------------------------------
class QueryLog:
    def __init__(self):
        self.statements: List[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def selects(self) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith("SELECT"))

    def most_common(self, n: int = 5):
        return Counter(re.sub(r"\s+", " ", s)[:90] for s in self.statements).most_common(n)

    def __repr__(self) -> str:
        return f"<QueryLog {self.count} statements: {self.most_common(3)}>"


@contextmanager
def count_queries(manager) -> Iterator[QueryLog]:
    """Record every SQL statement the manager's engine executes inside the block."""
    from sqlalchemy import event

    log = QueryLog()

    def before(_conn, _cursor, statement, _params, _context, _many):
        log.statements.append(statement)

    event.listen(manager.engine, "before_cursor_execute", before)
    try:
        yield log
    finally:
        event.remove(manager.engine, "before_cursor_execute", before)


# ---------------------------------------------------------------------------
# Signals and menus
# ---------------------------------------------------------------------------
@contextmanager
def recording(*signals):
    """Collect the arguments of every emission of ``signals`` (disconnected afterwards)."""
    calls: List[tuple] = []
    slots = []
    for sig in signals:
        def slot(*args, _sig=sig):
            calls.append(args)
        sig.connect(slot)
        slots.append((sig, slot))
    try:
        yield calls
    finally:
        for sig, slot in slots:
            sig.disconnect(slot)


class MenuCatcher:
    """Context menus that don't block: ``install(monkeypatch, *modules)`` swaps the ``QMenu``
    those modules use for a subclass whose ``exec`` records the menu (patching ``QMenu.exec``
    itself has no effect - Shiboken calls the C++ method directly). ``trigger(text)`` fires
    the action whose text starts with ``text``, in submenus too."""

    def __init__(self):
        self.menus: list = []
        from PySide6.QtWidgets import QMenu

        catcher = self

        class CatchingMenu(QMenu):
            def exec(self, *args, **kwargs):
                catcher.menus.append(self)
                return None

        self.menu_class = CatchingMenu

    def install(self, monkeypatch, *modules) -> "MenuCatcher":
        for module in modules:
            monkeypatch.setattr(module, "QMenu", self.menu_class)
        return self

    @property
    def last(self):
        return self.menus[-1]

    def texts(self, menu=None) -> List[str]:
        menu = menu or self.last
        return [a.text() for a in menu.actions() if not a.isSeparator()]

    def find(self, text: str, menu=None):
        menu = menu or self.last
        for action in menu.actions():
            if action.text().startswith(text):
                return action
            if action.menu() is not None:
                found = self.find(text, action.menu())
                if found is not None:
                    return found
        return None

    def trigger(self, text: str, menu=None) -> None:
        action = self.find(text, menu)
        assert action is not None, f"no menu action {text!r} in {self.texts(menu)}"
        action.trigger()
