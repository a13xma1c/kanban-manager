"""
Views Package for KanbanManager
"""
from kanbanmanager.ui.views.board_view import BoardView
from kanbanmanager.ui.views.calendar_view import CalendarView
from kanbanmanager.ui.views.timeline_view import TimelineView
from kanbanmanager.ui.views.analytics_view import AnalyticsView
from kanbanmanager.ui.views.workload_view import WorkloadView

__all__ = [
    "BoardView",
    "CalendarView",
    "TimelineView",
    "AnalyticsView",
    "WorkloadView",
]
