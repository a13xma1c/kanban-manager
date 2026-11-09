"""
Application Entry Point for KanbanManager
"""
import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from kanbanmanager.config import APP_NAME
from kanbanmanager.database.connection import get_db_manager
from kanbanmanager.ui.main_window import MainWindow


def main() -> int:
    """Initialize application, database, and start the Qt event loop."""
    # Enable High DPI scaling
    QApplication.setApplicationName(APP_NAME)
    QApplication.setOrganizationName("KanbanManager")

    app = QApplication(sys.argv)

    # Initialize SQLite Database & seed defaults
    db_manager = get_db_manager()
    db_manager.init_database(seed_defaults=True)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
