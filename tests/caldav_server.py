"""
A small in-process CalDAV / iCalendar test server (not a test module).

Supports what the sync engine uses: PROPFIND (principal -> calendar home ->
calendars), REPORT calendar-query with calendar-data, GET, PUT (ETag,
If-Match, If-None-Match) and DELETE, behind HTTP Basic auth. Plain GET of
/feed.ics serves a settable iCalendar feed.
"""
from __future__ import annotations

import base64
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

USER, PASSWORD = "ann", "s3cret"
PRINCIPAL = "/principals/ann/"
HOME = "/cal/"
TASKS = "/cal/tasks/"
EVENTS = "/cal/events/"


class CalDAVState:
    def __init__(self):
        self.items: Dict[str, Tuple[str, str]] = {}      # href -> (etag, text)
        self.feed = ""
        self.feed_auth = False
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []
        self.lock = threading.Lock()
        self._n = 0

    def etag(self, text: str) -> str:
        self._n += 1
        return '"' + hashlib.md5(f"{self._n}{text}".encode()).hexdigest()[:12] + '"'

    def put(self, href: str, text: str) -> str:
        with self.lock:
            tag = self.etag(text)
            self.items[href] = (tag, text)
            return tag

    def texts(self, collection: str = TASKS) -> Dict[str, str]:
        return {h: t for h, (_e, t) in self.items.items() if h.startswith(collection)}


def _ms(responses: str) -> bytes:
    return (f'<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:" '
            f'xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/">'
            f'{responses}</d:multistatus>').encode()


def _resp(href: str, props: str) -> str:
    return (f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
            f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def _cal_props(name: str, comp: str) -> str:
    return (f"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>{name}</d:displayname>"
            f'<c:supported-calendar-component-set><c:comp name="{comp}"/></c:supported-calendar-component-set>')


def make_handler(state: CalDAVState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            ok = header == "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            if not ok:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="test"')
                self.send_header("Content-Length", "0")
                self.end_headers()
            return ok

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _send(self, code: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None,
                  ctype: str = "application/xml; charset=utf-8") -> None:
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _log(self, method: str) -> None:
            state.requests.append((method, self.path, {k: v for k, v in self.headers.items()}))

        def do_GET(self):
            self._log("GET")
            if self.path.startswith("/feed.ics"):
                if state.feed_auth and not self._authorized():
                    return
                self._send(200, state.feed.encode("utf-8"), ctype="text/calendar; charset=utf-8")
                return
            if not self._authorized():
                return
            item = state.items.get(self.path)
            if item is None:
                self._send(404)
            else:
                self._send(200, item[1].encode(), {"ETag": item[0]}, "text/calendar; charset=utf-8")

        def do_PROPFIND(self):
            self._log("PROPFIND")
            self._body()
            if not self._authorized():
                return
            depth = self.headers.get("Depth", "0")
            if self.path in ("/", "/.well-known/caldav", "/dav/"):
                body = _ms(_resp(self.path, f"<d:current-user-principal><d:href>{PRINCIPAL}</d:href>"
                                            f"</d:current-user-principal><d:resourcetype><d:collection/></d:resourcetype>"))
            elif self.path == PRINCIPAL:
                body = _ms(_resp(PRINCIPAL, f"<c:calendar-home-set><d:href>{HOME}</d:href></c:calendar-home-set>"))
            elif self.path == HOME:
                parts = _resp(HOME, "<d:resourcetype><d:collection/></d:resourcetype>")
                if depth == "1":
                    parts += _resp(TASKS, _cal_props("Tasks", "VTODO")) + _resp(EVENTS, _cal_props("Events", "VEVENT"))
                body = _ms(parts)
            elif self.path == TASKS:
                body = _ms(_resp(TASKS, _cal_props("Tasks", "VTODO")))
            elif self.path == EVENTS:
                body = _ms(_resp(EVENTS, _cal_props("Events", "VEVENT")))
            else:
                self._send(404)
                return
            self._send(207, body)

        def do_REPORT(self):
            self._log("REPORT")
            query = self._body().decode()
            if not self._authorized():
                return
            comp = "VEVENT" if 'name="VEVENT"' in query else "VTODO"
            with state.lock:
                items = [(h, e, t) for h, (e, t) in state.items.items() if h.startswith(self.path)
                         and f"BEGIN:{comp}" in t]
            parts = "".join(_resp(h, f"<d:getetag>{escape(e)}</d:getetag><c:calendar-data>{escape(t)}"
                                     f"</c:calendar-data>") for h, e, t in items)
            self._send(207, _ms(parts))

        def do_PUT(self):
            self._log("PUT")
            body = self._body().decode()
            if not self._authorized():
                return
            with state.lock:
                existing = state.items.get(self.path)
                if self.headers.get("If-None-Match") == "*" and existing is not None:
                    self._send(412)
                    return
                match = self.headers.get("If-Match")
                if match and (existing is None or existing[0] != match):
                    self._send(412)
                    return
            tag = state.put(self.path, body)
            self._send(201 if existing is None else 204, headers={"ETag": tag})

        def do_DELETE(self):
            self._log("DELETE")
            if not self._authorized():
                return
            with state.lock:
                existing = state.items.get(self.path)
                match = self.headers.get("If-Match")
                if existing is None:
                    self._send(404)
                    return
                if match and existing[0] != match:
                    self._send(412)
                    return
                del state.items[self.path]
            self._send(204)

    return Handler


class Server:
    def __init__(self):
        self.state = CalDAVState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
