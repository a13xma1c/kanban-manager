"""
Single-Instance IPC over a local socket

The running app listens on a per-user, per-database local socket. The command
line uses it so any OS-level shortcut can drive quick capture:

    kanbanmanager --capture              # pop up the capture window in the running app
    kanbanmanager --capture "Call Ana tomorrow 3pm"   # capture straight away
    kanbanmanager                        # (second launch) brings the existing window forward

Messages are one JSON object per connection: {"cmd": "capture" | "show", "text": "..."}.
"""
from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def server_name(db_path: Path | str) -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    digest = hashlib.sha1(str(Path(db_path).resolve()).encode()).hexdigest()[:10]
    return f"kanbanmanager-{user}-{digest}"


def send_command(name: str, payload: Dict[str, Any], timeout_ms: int = 1500) -> bool:
    """Deliver a command to a running instance. False if none is listening."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    data = json.dumps(payload).encode("utf-8")
    written = sock.write(data)
    sock.flush()  # local sockets usually write everything right here...
    if sock.bytesToWrite() > 0:
        sock.waitForBytesWritten(timeout_ms)  # ...otherwise wait for the rest
    ok = written == len(data) and sock.bytesToWrite() == 0
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    return ok


class CommandServer(QObject):
    command_received = Signal(dict)

    def __init__(self, name: str, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.name = name
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_connection)
        self._buffers: Dict[QLocalSocket, bytes] = {}

    def start(self) -> bool:
        """Listen; refuses to steal the socket of another live instance."""
        probe = QLocalSocket()
        probe.connectToServer(self.name)
        if probe.waitForConnected(200):
            probe.disconnectFromServer()
            return False
        QLocalServer.removeServer(self.name)  # clean up a stale socket left by a crash
        return self.server.listen(self.name)

    def stop(self) -> None:
        self.server.close()

    def _on_connection(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._finish(s))

    def _read(self, sock: QLocalSocket) -> None:
        self._buffers[sock] = self._buffers.get(sock, b"") + bytes(sock.readAll())

    def _finish(self, sock: QLocalSocket) -> None:
        self._read(sock)
        raw = self._buffers.pop(sock, b"")
        sock.deleteLater()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return
        if isinstance(payload, dict) and payload.get("cmd") in ("capture", "show"):
            self.command_received.emit(payload)
