"""
Database Connection and Session Management
"""
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from kanbanmanager.config import DEFAULT_DB_PATH, DEFAULT_LABEL_PALETTE, DEFAULT_COLUMNS
from kanbanmanager.database.attachments import AttachmentStore
from kanbanmanager.database.models import Base, Workspace, Board, BoardColumn, Label, Task
from kanbanmanager.enums import Priority, ColumnType

# Columns added to pre-existing tables after the initial schema. create_all() only
# creates missing *tables*, so these are applied with ALTER TABLE on startup/restore.
_ADDED_COLUMNS = {
    "tasks": [
        ("assignee", "VARCHAR(120)"),
    ],
    "activity_logs": [
        ("actor", "VARCHAR(120)"),
        ("field_name", "VARCHAR(50)"),
        ("old_value", "TEXT"),
        ("new_value", "TEXT"),
    ],
}
_ADDED_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_activity_task_time ON activity_logs (task_id, timestamp)",
]
# Before time entries existed, "spent hours" was a bare number on the task. Turn any such
# value into a single manual entry so totals stay consistent (idempotent: only tasks
# that have spent hours but no entries at all).
_BACKFILL_SPENT_HOURS = """
INSERT INTO time_entries (task_id, user, started_at, ended_at, duration_seconds, source, note, created_at)
SELECT t.id, NULL,
       datetime(t.updated_at, printf('-%d seconds', CAST(ROUND(t.spent_hours * 3600) AS INTEGER))),
       t.updated_at, CAST(ROUND(t.spent_hours * 3600) AS INTEGER), 'MANUAL',
       'Time spent recorded before time tracking', CURRENT_TIMESTAMP
FROM tasks t
WHERE COALESCE(t.spent_hours, 0) > 0
  AND NOT EXISTS (SELECT 1 FROM time_entries e WHERE e.task_id = t.id)
"""


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable foreign key constraints for SQLite connections."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


class DatabaseManager:
    """Manages SQLite connection, engine, migrations, and session lifecycle."""

    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            self.db_path = DEFAULT_DB_PATH
        else:
            self.db_path = Path(db_path)

        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.db_url = f"sqlite:///{self.db_path}"
        else:
            self.db_url = "sqlite:///:memory:"

        self.engine = create_engine(
            self.db_url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

        # Attachment files live beside the database (one store per database file)
        if self.db_url == "sqlite:///:memory:":
            self.attachments_dir = Path(tempfile.mkdtemp(prefix="kanban_attachments_"))
        else:
            self.attachments_dir = self.db_path.parent / f"{self.db_path.stem}_attachments"
        self.attachment_store = AttachmentStore(self.attachments_dir)

    def init_database(self, seed_defaults: bool = True) -> None:
        """Create tables, apply additive migrations, and seed sample data if empty."""
        Base.metadata.create_all(self.engine)
        self._migrate_schema()
        if seed_defaults:
            with self.session_scope() as session:
                self._seed_default_data_if_empty(session)

    def _migrate_schema(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table, columns in _ADDED_COLUMNS.items():
                if not insp.has_table(table):
                    continue
                existing = {c["name"] for c in insp.get_columns(table)}
                for name, ddl in columns:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            for stmt in _ADDED_INDEXES:
                conn.execute(text(stmt))
            conn.execute(text(_BACKFILL_SPENT_HOURS))

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope around a series of operations."""
        session = self.SessionFactory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _seed_default_data_if_empty(self, session: Session) -> None:
        """Seed a clean default workspace, board, and starter tasks if DB is empty."""
        existing_ws = session.query(Workspace).first()
        if existing_ws is not None:
            return

        # Create Default Workspace
        ws = Workspace(
            name="My Workspace",
            description="Default workspace for projects and task tracking",
            color="#6366F1",
            icon="folder",
        )
        session.add(ws)
        session.flush()

        # Create Default Kanban Board
        board = Board(
            workspace_id=ws.id,
            name="Product Roadmap",
            description="Main project board with standard Kanban workflow",
            color="#3B82F6",
            icon="layout",
            position=0,
        )
        session.add(board)
        session.flush()

        # Create Columns
        created_cols = []
        for i, col_def in enumerate(DEFAULT_COLUMNS):
            col = BoardColumn(
                board_id=board.id,
                name=col_def["name"],
                position=i,
                wip_limit=col_def["wip_limit"],
                color=col_def["color"],
                column_type=col_def["type"],
                is_done_column=col_def.get("is_done_column", False),
            )
            session.add(col)
            created_cols.append(col)
        session.flush()

        # Create Default Labels
        created_labels = {}
        for name, color in DEFAULT_LABEL_PALETTE:
            lbl = Label(board_id=board.id, name=name, color=color)
            session.add(lbl)
            created_labels[name] = lbl
        session.flush()

        # Create Sample Starter Tasks
        t1 = Task(
            board_id=board.id,
            column_id=created_cols[1].id,  # To Do
            title="Design system architecture & database schema",
            description="Set up relational models for workspaces, boards, custom workflows, and task dependencies.",
            position=0,
            priority=Priority.HIGH.value,
            estimated_hours=4.0,
            labels=[created_labels["Feature"], created_labels["Improvement"]],
            checklist=[
                {"id": 1, "text": "Draft schema diagrams", "done": True},
                {"id": 2, "text": "Implement SQLAlchemy models", "done": True},
                {"id": 3, "text": "Add SQLite foreign key pragma", "done": True},
            ],
        )
        t2 = Task(
            board_id=board.id,
            column_id=created_cols[2].id,  # In Progress
            title="Implement Drag and Drop Kanban Board",
            description="Create custom Qt widgets with seamless MIME data drag-and-drop between columns and reordering within columns.",
            position=0,
            priority=Priority.URGENT.value,
            estimated_hours=6.0,
            labels=[created_labels["Feature"]],
            checklist=[
                {"id": 1, "text": "Task card widget with badges", "done": True},
                {"id": 2, "text": "Column widget with WIP indicator", "done": False},
                {"id": 3, "text": "Visual drop indicator and reordering", "done": False},
            ],
        )
        t3 = Task(
            board_id=board.id,
            column_id=created_cols[0].id,  # Backlog
            title="Calendar and Timeline Gantt views",
            description="Build interactive month/week calendar view and timeline Gantt chart with dependency link lines.",
            position=0,
            priority=Priority.MEDIUM.value,
            estimated_hours=8.0,
            labels=[created_labels["Feature"], created_labels["Design"]],
        )
        t4 = Task(
            board_id=board.id,
            column_id=created_cols[4].id,  # Done
            title="Project setup and environment configuration",
            description="Initialized uv environment with PySide6 and SQLAlchemy.",
            position=0,
            priority=Priority.LOW.value,
            is_completed=True,
            completed_at=datetime.now(),
            labels=[created_labels["Documentation"]],
        )
        session.add_all([t1, t2, t3, t4])
        session.flush()

        # Second board: a guided tour of rich descriptions, attachments, comments & history
        from kanbanmanager.database.demo_board import build_demo_board

        build_demo_board(session, ws.id, self.attachment_store)


# Global DB Manager Instance
_db_manager: Optional[DatabaseManager] = None


def get_db_manager(db_path: Optional[Path | str] = None) -> DatabaseManager:
    """Get or initialize the singleton DatabaseManager instance."""
    global _db_manager
    if _db_manager is None or db_path is not None:
        _db_manager = DatabaseManager(db_path)
    return _db_manager
