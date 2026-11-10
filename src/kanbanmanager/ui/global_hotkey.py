"""
System-Wide Hotkey (works while other applications have focus)

Qt has no API for global shortcuts, so this uses the platform directly:

* **Windows**: ``RegisterHotKey`` + a native event filter for ``WM_HOTKEY``.
* **Linux / X11**: ``XGrabKey`` on the root window through a separate Xlib
  connection, polled by a timer (independent of Qt's platform plugin). All
  NumLock / CapsLock variants are grabbed; ``BadAccess`` (another program owns
  the combination) is reported.
* **Wayland and macOS**: applications may not grab keys globally. Bind your
  desktop's own shortcut to ``kanbanmanager --capture`` instead; it reaches the
  running app through a local socket (see ``ui/ipc.py``).

``GlobalHotkey.start(sequence)`` returns ``(ok, message)``; ``activated`` fires
on every press.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from typing import List, Optional, Set, Tuple

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, QTimer, Signal

DEFAULT_HOTKEY = "Ctrl+Shift+Space"

MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt", "option": "alt",
             "meta": "super", "super": "super", "win": "super", "cmd": "super"}


def parse_hotkey(sequence: str) -> Tuple[Set[str], str]:
    """'Ctrl+Shift+Space' -> ({'ctrl', 'shift'}, 'space'). Raises ValueError when invalid."""
    parts = [p.strip().lower() for p in (sequence or "").replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty shortcut")
    *mods, key = parts
    modifiers = set()
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(f"Unknown modifier '{m}'")
        modifiers.add(MODIFIERS[m])
    if key in MODIFIERS:
        raise ValueError("A shortcut needs a key besides the modifiers")
    if not modifiers:
        raise ValueError("Use at least one modifier (Ctrl, Alt, Shift or Super) so typing isn't hijacked")
    named = {"space", "return", "enter", "tab", "escape", "esc", "backspace", "insert", "delete", "home", "end",
             "pageup", "pagedown", "up", "down", "left", "right"}
    if not (len(key) == 1 and key.isalnum()) and key not in named and not (
            key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24):
        raise ValueError(f"Unsupported key '{key}'")
    return modifiers, key


def session_kind() -> str:
    """'windows', 'macos', 'x11', 'wayland' or 'unknown'."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


CLI_HINT = ("Bind a keyboard shortcut in your system settings to the command:  kanbanmanager --capture")


# ---------------------------------------------------------------------------
# X11
# ---------------------------------------------------------------------------
class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int), ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint), ("same_screen", ctypes.c_int),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("xkey", _XKeyEvent), ("pad", ctypes.c_long * 24)]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("display", ctypes.c_void_p), ("resourceid", ctypes.c_ulong),
                ("serial", ctypes.c_ulong), ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


_X_KEYSYMS = {"space": "space", "return": "Return", "enter": "Return", "tab": "Tab", "escape": "Escape",
              "esc": "Escape", "backspace": "BackSpace", "insert": "Insert", "delete": "Delete", "home": "Home",
              "end": "End", "pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left",
              "right": "Right"}
_X_MODS = {"shift": 1 << 0, "ctrl": 1 << 2, "alt": 1 << 3, "super": 1 << 6}
_X_LOCK_VARIANTS = [0, 1 << 1, 1 << 4, (1 << 1) | (1 << 4)]  # CapsLock, NumLock (Mod2)
_X_KEYPRESS = 2
_X_BAD_ACCESS = 10

_ErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))


class _X11Backend:
    name = "X11"

    def __init__(self, callback):
        self.callback = callback
        self.xlib = None
        self.display = None
        self.keycode = 0
        self.mask = 0
        self._errors: List[int] = []
        self._handler = _ErrorHandler(self._on_error)  # keep a reference (ctypes callback)
        self._timer: Optional[QTimer] = None

    def _on_error(self, _display, event) -> int:
        self._errors.append(event.contents.error_code)
        return 0

    def start(self, modifiers: Set[str], key: str) -> Tuple[bool, str]:
        path = ctypes.util.find_library("X11")
        if not path:
            return False, "libX11 is not available"
        x = ctypes.cdll.LoadLibrary(path)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int]
        x.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XSetErrorHandler.argtypes = [_ErrorHandler]
        x.XSetErrorHandler.restype = ctypes.c_void_p
        display = x.XOpenDisplay(None)
        if not display:
            return False, "Could not connect to the X server"
        keysym = x.XStringToKeysym(_X_KEYSYMS.get(key, key.upper() if key.startswith("f") else key).encode())
        keycode = x.XKeysymToKeycode(display, keysym) if keysym else 0
        if not keycode:
            x.XCloseDisplay(display)
            return False, f"The key '{key}' is not on this keyboard layout"
        self.xlib, self.display, self.keycode = x, display, keycode
        self.mask = sum(_X_MODS[m] for m in modifiers)
        x.XSetErrorHandler(self._handler)
        root = x.XDefaultRootWindow(display)
        self._errors.clear()
        for extra in _X_LOCK_VARIANTS:
            x.XGrabKey(display, keycode, self.mask | extra, root, 1, 1, 1)  # GrabModeAsync = 1
        x.XSync(display, 0)
        if _X_BAD_ACCESS in self._errors:
            self.stop()
            return False, "Another application already uses this shortcut"
        self._root = root
        self._timer = QTimer()
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        return True, "System-wide shortcut active (X11)"

    def _poll(self) -> None:
        if not self.display:
            return
        event = _XEvent()
        fired = False
        while self.xlib.XPending(self.display):
            self.xlib.XNextEvent(self.display, ctypes.byref(event))
            if event.type == _X_KEYPRESS and event.xkey.keycode == self.keycode:
                fired = True
        if fired:
            self.callback()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.display:
            root = self.xlib.XDefaultRootWindow(self.display)
            for extra in _X_LOCK_VARIANTS:
                self.xlib.XUngrabKey(self.display, self.keycode, self.mask | extra, root)
            self.xlib.XSync(self.display, 0)
            self.xlib.XCloseDisplay(self.display)
            self.display = None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
_WIN_MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "super": 0x8}
_WIN_NOREPEAT = 0x4000
_WIN_KEYS = {"space": 0x20, "return": 0x0D, "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
             "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21,
             "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
_WM_HOTKEY = 0x0312


class _WinFilter(QAbstractNativeEventFilter):
    def __init__(self, hotkey_id: int, callback):
        super().__init__()
        self.hotkey_id = hotkey_id
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG" or event_type == "windows_generic_MSG":
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY and msg.wParam == self.hotkey_id:
                self.callback()
                return True, 0
        return False, 0


class _WindowsBackend:
    name = "Windows"
    HOTKEY_ID = 0x4B4D  # "KM"

    def __init__(self, callback):
        self.callback = callback
        self.filter: Optional[_WinFilter] = None

    def start(self, modifiers: Set[str], key: str) -> Tuple[bool, str]:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if key in _WIN_KEYS:
            vk = _WIN_KEYS[key]
        elif len(key) == 1:
            vk = ord(key.upper())
        else:
            vk = 0x70 + int(key[1:]) - 1  # F1..F24
        mods = sum(_WIN_MODS[m] for m in modifiers) | _WIN_NOREPEAT
        if not user32.RegisterHotKey(None, self.HOTKEY_ID, mods, vk):
            return False, "Another application already uses this shortcut"
        self.filter = _WinFilter(self.HOTKEY_ID, self.callback)
        QCoreApplication.instance().installNativeEventFilter(self.filter)
        return True, "System-wide shortcut active (Windows)"

    def stop(self) -> None:
        if self.filter is not None:
            ctypes.windll.user32.UnregisterHotKey(None, self.HOTKEY_ID)  # type: ignore[attr-defined]
            QCoreApplication.instance().removeNativeEventFilter(self.filter)
            self.filter = None


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------
class GlobalHotkey(QObject):
    activated = Signal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._backend = None
        self.sequence = ""
        self.active = False
        self.status = "Not started"

    def start(self, sequence: str = DEFAULT_HOTKEY) -> Tuple[bool, str]:
        self.stop()
        try:
            modifiers, key = parse_hotkey(sequence)
        except ValueError as exc:
            self.status = str(exc)
            return False, self.status
        kind = session_kind()
        if kind == "windows":
            backend = _WindowsBackend(self.activated.emit)
        elif kind == "x11":
            backend = _X11Backend(self.activated.emit)
        elif kind == "wayland":
            self.status = f"Wayland doesn't let apps register system-wide shortcuts. {CLI_HINT}"
            return False, self.status
        elif kind == "macos":
            self.status = f"macOS system-wide shortcuts aren't built in yet. {CLI_HINT}"
            return False, self.status
        else:
            self.status = "No graphical session detected"
            return False, self.status
        try:
            ok, message = backend.start(modifiers, key)
        except Exception as exc:  # a native failure must never take down the app
            ok, message = False, f"Could not register the shortcut: {exc}"
        if ok:
            self._backend, self.sequence, self.active = backend, sequence, True
        self.status = message
        return ok, message

    def stop(self) -> None:
        if self._backend is not None:
            try:
                self._backend.stop()
            finally:
                self._backend = None
        self.active = False
