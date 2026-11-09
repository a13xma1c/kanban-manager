"""
Modern Dark & Light Theme Stylesheets and Design System for KanbanManager
"""

DARK_THEME_QSS = """
/* ========================================================================= */
/* Global Reset & Base Styles                                                */
/* ========================================================================= */
QMainWindow, QDialog, QWidget {
    background-color: #0F172A; /* Deep Slate Navy */
    color: #F8FAFC;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

/* Tooltips */
QToolTip {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}

/* ========================================================================= */
/* Buttons                                                                   */
/* ========================================================================= */
QPushButton {
    background-color: #1E293B;
    color: #F1F5F9;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 14px;
    font-weight: 500;
}

QPushButton:hover {
    background-color: #334155;
    border-color: #475569;
}

QPushButton:pressed {
    background-color: #0F172A;
}

QPushButton:disabled {
    background-color: #1E293B;
    color: #64748B;
    border-color: #1E293B;
}

QPushButton#PrimaryButton {
    background-color: #3B82F6;
    color: #FFFFFF;
    border: 1px solid #2563EB;
    font-weight: 600;
}

QPushButton#PrimaryButton:hover {
    background-color: #2563EB;
}

QPushButton#PrimaryButton:pressed {
    background-color: #1D4ED8;
}

QPushButton#SuccessButton {
    background-color: #10B981;
    color: #FFFFFF;
    border: 1px solid #059669;
    font-weight: 600;
}

QPushButton#SuccessButton:hover {
    background-color: #059669;
}

QPushButton#DangerButton {
    background-color: #EF4444;
    color: #FFFFFF;
    border: 1px solid #DC2626;
    font-weight: 600;
}

QPushButton#DangerButton:hover {
    background-color: #DC2626;
}

QPushButton#IconButton {
    background-color: transparent;
    border: none;
    border-radius: 4px;
    padding: 4px;
}

QPushButton#IconButton:hover {
    background-color: #334155;
}

/* ========================================================================= */
/* Inputs, Combos, SpinBoxes                                                 */
/* ========================================================================= */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QDateTimeEdit {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 10px;
    selection-background-color: #3B82F6;
}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus {
    border: 1px solid #3B82F6;
    background-color: #1E293B;
}

QComboBox::drop-down {
    border: none;
    width: 24px;
}

QComboBox QAbstractItemView {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #334155;
    selection-background-color: #3B82F6;
    selection-color: #FFFFFF;
    padding: 4px;
}

/* ========================================================================= */
/* ScrollBars                                                                */
/* ========================================================================= */
QScrollBar:vertical {
    border: none;
    background: #0F172A;
    width: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar::handle:vertical {
    background: #334155;
    min-height: 24px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #475569;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QScrollBar:horizontal {
    border: none;
    background: #0F172A;
    height: 8px;
    margin: 0px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal {
    background: #334155;
    min-width: 24px;
    border-radius: 4px;
}

QScrollBar::handle:horizontal:hover {
    background: #475569;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0px;
}

/* ========================================================================= */
/* Lists, Trees, Tables                                                      */
/* ========================================================================= */
QListWidget, QTreeWidget, QTableWidget {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 4px;
}

QListWidget::item, QTreeWidget::item {
    padding: 8px 10px;
    border-radius: 6px;
    margin: 2px 0px;
}

QListWidget::item:hover, QTreeWidget::item:hover {
    background-color: #334155;
}

QListWidget::item:selected, QTreeWidget::item:selected {
    background-color: #2563EB;
    color: #FFFFFF;
}

/* ========================================================================= */
/* Menus                                                                     */
/* ========================================================================= */
QMenu {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #3B82F6;
    color: #FFFFFF;
}

QMenu::separator {
    height: 1px;
    background: #334155;
    margin: 4px 8px;
}

/* ========================================================================= */
/* Tabs                                                                      */
/* ========================================================================= */
QTabWidget::pane {
    border: 1px solid #334155;
    background: #1E293B;
    border-radius: 8px;
}

QTabBar::tab {
    background: #0F172A;
    color: #94A3B8;
    border: 1px solid #334155;
    border-bottom: none;
    padding: 8px 16px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}

QTabBar::tab:selected {
    background: #1E293B;
    color: #F8FAFC;
    font-weight: 600;
}

/* ========================================================================= */
/* Custom Components Styling                                                 */
/* ========================================================================= */
#SidebarWidget {
    background-color: #111827;
    border-right: 1px solid #1F2937;
}

#TopHeaderBar {
    background-color: #111827;
    border-bottom: 1px solid #1F2937;
    padding: 8px 16px;
}

#ColumnHeader {
    background-color: #1E293B;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 10px 12px;
}

#ColumnContainer {
    background-color: #182234;
    border: 1px solid #293548;
    border-radius: 10px;
}

#TaskCardWidget {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 10px;
}

#TaskCardWidget:hover {
    border-color: #3B82F6;
    background-color: #243046;
}

#TaskCardBlocked {
    border: 1px solid #EF4444;
    background-color: #2A1D24;
}

#StatsBarWidget {
    background-color: #111827;
    border-bottom: 1px solid #1F2937;
    padding: 4px 16px;
}
"""


def get_stylesheet() -> str:
    """Return the application QSS stylesheet."""
    return DARK_THEME_QSS
