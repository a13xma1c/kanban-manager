"""
Live calendar feed (a tiny HTTP server calendar apps subscribe to) and the
background runner for calendar subscriptions.

The feed serves iCalendar documents generated on every request::

    http://127.0.0.1:8765/<secret>/mine.ics       tasks assigned to you
    http://127.0.0.1:8765/<secret>/all.ics        every board
    http://127.0.0.1:8765/<secret>/board-<id>.ics one board

The secret token in the path is the only protection, so the server listens on
this computer only unless "allow other devices on the network" is switched on.
Calendar apps on this computer (Apple Calendar, Thunderbird, Outlook, GNOME
Calendar ...) can subscribe directly (``webcal://`` links); web calendars such
as Google Calendar can't reach a local address.

``SyncRunner`` runs subscriptions on a worker thread with its own database
session, so slow servers never freeze the window.
"""
from __future__ import annotations

import re
import secrets
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInterface, QTcpServer, QTcpSocket

from kanbanmanager.core import calendar_sync as cs
from kanbanmanager.database.service import DatabaseService

FEED_KEY = "calendar_feed"
DEFAULT_FEED: Dict[str, Any] = {
    "enabled": False,
    "port": 8765,
    "lan": False,
    "token": "",
    "kind": "VEVENT",              # VEVENT (shows in every calendar) | VTODO (to-do lists)
    "event_style": "all_day",      # all_day | timed
    "include_completed": False,
    "alarm_minutes": 0,
}
MAX_REQUEST = 8192


def load_feed_settings(service: DatabaseService) -> Dict[str, Any]:
    s = dict(DEFAULT_FEED)
    s.update({k: v for k, v in (service.get_setting(FEED_KEY, {}) or {}).items() if k in DEFAULT_FEED})
    if not s["token"]:
        s["token"] = secrets.token_urlsafe(18)
        service.set_setting(FEED_KEY, s)
    return s


def save_feed_settings(service: DatabaseService, settings: Dict[str, Any]) -> Dict[str, Any]:
    s = load_feed_settings(service)
    s.update({k: v for k, v in settings.items() if k in DEFAULT_FEED})
    s["port"] = max(1024, min(65535, int(s["port"] or DEFAULT_FEED["port"])))
    service.set_setting(FEED_KEY, s)
    return s


def new_token(service: DatabaseService) -> Dict[str, Any]:
    """Replace the secret (existing subscriptions stop working)."""
    return save_feed_settings(service, {"token": secrets.token_urlsafe(18)})


def feed_list(service: DatabaseService) -> List[Tuple[str, str]]:
    """(label, file name) of every feed."""
    out = [("My tasks", "mine.ics"), ("All boards", "all.ics")]
    for ws in service.get_workspaces():
        for b in ws.boards:
            if not b.is_archived:
                out.append((f"{b.name}  ·  {ws.name}", f"board-{b.id}.ics"))
    return out


def render_feed(service: DatabaseService, name: str, settings: Dict[str, Any]) -> Optional[str]:
    """The iCalendar text for a feed file name, or None if there's no such feed."""
    common = dict(kind=settings["kind"], include_completed=settings["include_completed"],
                  event_style=settings["event_style"], alarm_minutes=int(settings["alarm_minutes"] or 0),
                  refresh_minutes=15)
    if name == "mine.ics":
        return cs.export_calendar(service, person=service.actor, name="My tasks (KanbanManager)", **common)
    if name == "all.ics":
        return cs.export_calendar(service, name="KanbanManager", **common)
    m = re.fullmatch(r"board-(\d+)\.ics", name)
    if m:
        board = service.get_board(int(m.group(1)))
        if board is None or board.is_archived:
            return None
        return cs.export_calendar(service, [board.id], name=f"{board.name} (KanbanManager)", **common)
    return None


def lan_address() -> Optional[str]:
    for addr in QNetworkInterface.allAddresses():
        if addr.protocol() == QAbstractSocket.IPv4Protocol and not addr.isLoopback():
            return addr.toString()
    return None


class CalendarFeedServer(QObject):
    """Serves the feeds while the app runs."""

    request_served = Signal(str, int)   # path, status

    def __init__(self, service: DatabaseService, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.service = service
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._on_connection)
        self.settings = dict(DEFAULT_FEED)
        self.status = "Off"
        self._buffers: Dict[QTcpSocket, bytes] = {}

    @property
    def running(self) -> bool:
        return self.server.isListening()

    @property
    def port(self) -> int:
        return int(self.server.serverPort()) if self.running else int(self.settings["port"])

    def start(self, settings: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        self.stop()
        self.settings = settings or load_feed_settings(self.service)
        address = QHostAddress(QHostAddress.AnyIPv4) if self.settings["lan"] else QHostAddress(QHostAddress.LocalHost)
        if not self.server.listen(address, int(self.settings["port"])):
            self.status = f"Could not open port {self.settings['port']}: {self.server.errorString()}"
            return False, self.status
        where = "this computer and your network" if self.settings["lan"] else "this computer"
        self.status = f"Serving on port {self.port} ({where})"
        return True, self.status

    def stop(self) -> None:
        if self.server.isListening():
            self.server.close()
        self.status = "Off"

    def urls(self) -> List[Tuple[str, str, str]]:
        """(label, http URL, webcal URL) of every feed."""
        host = (lan_address() or "127.0.0.1") if self.settings["lan"] else "127.0.0.1"
        base = f"{host}:{self.port}/{self.settings['token']}"
        return [(label, f"http://{base}/{name}", f"webcal://{base}/{name}") for label, name in feed_list(self.service)]

    # HTTP ---------------------------------------------------------------------
    def _on_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(partial(self._on_ready, sock))
            sock.disconnected.connect(partial(self._on_gone, sock))

    def _on_gone(self, sock: QTcpSocket) -> None:
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def _on_ready(self, sock: QTcpSocket) -> None:
        if sock not in self._buffers:
            return
        data = self._buffers[sock] + bytes(sock.readAll())
        if len(data) > MAX_REQUEST:
            self._reply(sock, 431, b"Request too large", "text/plain", "")
            return
        self._buffers[sock] = data
        if b"\r\n\r\n" not in data and b"\n\n" not in data:
            return
        line = data.split(b"\n", 1)[0].decode("latin-1").strip()
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("GET", "HEAD"):
            self._reply(sock, 405, b"Method not allowed", "text/plain", line)
            return
        method, path = parts[0], parts[1].split("?", 1)[0]
        segments = [p for p in path.split("/") if p]
        if len(segments) != 2 or not secrets.compare_digest(segments[0], str(self.settings["token"])):
            self._reply(sock, 404, b"Not found", "text/plain", path)
            return
        try:
            text = render_feed(self.service, segments[1], self.settings)
        except Exception as exc:  # a broken feed must not take the app down
            self._reply(sock, 500, f"Error: {exc}".encode(), "text/plain", path)
            return
        if text is None:
            self._reply(sock, 404, b"Not found", "text/plain", path)
            return
        body = text.encode("utf-8")
        self._reply(sock, 200, b"" if method == "HEAD" else body, "text/calendar; charset=utf-8", path,
                    length=len(body))

    def _reply(self, sock: QTcpSocket, status: int, body: bytes, ctype: str, path: str,
               length: Optional[int] = None) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed", 431: "Request Header Fields Too Large",
                  500: "Internal Server Error"}.get(status, "")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: "
                f"{length if length is not None else len(body)}\r\nCache-Control: no-cache\r\n"
                f"Connection: close\r\n\r\n").encode()
        self._buffers.pop(sock, None)
        sock.write(head + body)
        sock.flush()
        sock.disconnectFromHost()
        self.request_served.emit(path, status)


# ---------------------------------------------------------------------------
# Background sync
# ---------------------------------------------------------------------------
class _SyncThread(QThread):
    done = Signal(object)   # List[SyncReport]

    def __init__(self, db_manager, actor: str, sub_ids: Optional[List[int]], parent=None):
        super().__init__(parent)
        self.db_manager = db_manager
        self.actor = actor
        self.sub_ids = sub_ids

    def run(self) -> None:
        service = DatabaseService(self.db_manager, actor=self.actor)  # own sessions on this thread
        engine = cs.CalendarSync(service)
        try:
            reports = engine.sync_due() if self.sub_ids is None else [engine.sync(i) for i in self.sub_ids]
        except Exception as exc:  # pragma: no cover - CalendarSync already reports its own errors
            reports = []
            cs.log.exception("sync thread failed: %s", exc)
        self.done.emit(reports)


class SyncRunner(QObject):
    """Runs subscriptions off the UI thread; one run at a time."""

    finished = Signal(object)   # List[SyncReport]
    started = Signal()

    def __init__(self, service: DatabaseService, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.service = service
        self._thread: Optional[_SyncThread] = None
        self._queued: Optional[List[int]] = None

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def run(self, sub_ids: Optional[List[int]] = None) -> bool:
        """Sync the given subscriptions (None = the ones that are due). False if already busy."""
        if self._thread is not None:
            if sub_ids:
                self._queued = sorted(set((self._queued or []) + list(sub_ids)))
            return False
        self._thread = _SyncThread(self.service.db, self.service.actor, sub_ids, self)
        self._thread.done.connect(self._on_done)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()
        self.started.emit()
        return True

    def _on_done(self, reports) -> None:
        self._thread = None
        self.finished.emit(reports)
        if self._queued:
            queued, self._queued = self._queued, None
            self.run(queued)

    def wait(self, ms: int = 30000) -> bool:
        """Block until the current run is over (tests, shutdown)."""
        thread = self._thread
        return thread.wait(ms) if thread is not None else True
