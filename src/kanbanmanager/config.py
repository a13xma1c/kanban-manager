"""
Configuration and Application Constants for KanbanManager
"""
import getpass
import os
import sys
from pathlib import Path


APP_NAME = "KanbanManager"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Alex Maican"

# Base Data Directory: default to ~/.kanban_manager or environment variable
DEFAULT_DATA_DIR = Path(os.environ.get("KANBAN_DATA_DIR", Path.home() / ".kanban_manager"))
DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "kanban.db"
DEFAULT_BACKUP_DIR = DEFAULT_DATA_DIR / "backups"
DEFAULT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# Attachments: per-file size cap and file types rendered inline as images
MAX_ATTACHMENT_SIZE_MB = 25
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}


def get_current_user() -> str:
    """Display name used as comment author and audit-trail actor.

    Override with the KANBAN_USER environment variable; defaults to the OS login name.
    """
    name = os.environ.get("KANBAN_USER", "").strip()
    if name:
        return name
    try:
        return getpass.getuser() or "You"
    except Exception:
        return "You"


# Default Color Palettes
DEFAULT_WORKSPACE_COLORS = [
    "#6366F1",  # Indigo
    "#3B82F6",  # Blue
    "#06B6D4",  # Cyan
    "#10B981",  # Emerald
    "#F59E0B",  # Amber
    "#EC4899",  # Pink
    "#8B5CF6",  # Purple
    "#64748B",  # Slate
]

DEFAULT_LABEL_PALETTE = [
    ("Bug", "#EF4444"),
    ("Feature", "#3B82F6"),
    ("Improvement", "#10B981"),
    ("Documentation", "#8B5CF6"),
    ("Refactor", "#F59E0B"),
    ("Design", "#EC4899"),
    ("Urgent", "#DC2626"),
    ("Testing", "#06B6D4"),
]

DEFAULT_COLUMNS = [
    {"name": "Backlog", "color": "#64748B", "type": "BACKLOG", "wip_limit": 0},
    {"name": "To Do", "color": "#3B82F6", "type": "TODO", "wip_limit": 0},
    {"name": "In Progress", "color": "#F59E0B", "type": "IN_PROGRESS", "wip_limit": 3},
    {"name": "In Review", "color": "#8B5CF6", "type": "REVIEW", "wip_limit": 2},
    {"name": "Done", "color": "#10B981", "type": "DONE", "wip_limit": 0, "is_done_column": True},
]
