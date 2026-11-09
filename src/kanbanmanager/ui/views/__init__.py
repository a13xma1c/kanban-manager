"""
Views Package for KanbanManager
"""
from kanbanmanager.ui.views.board_view import BoardView
from kanbanmanager.ui.views.calendar_view import CalendarView
from kanbanmanager.ui.views.timeline_view import TimelineView
from kanbanmanager.ui.views.analytics_view import AnalyticsView

__all__ = [
    "BoardView",
    "CalendarView",
    "TimelineView",
    "AnalyticsView",
]
