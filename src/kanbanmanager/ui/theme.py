"""
Theme engine: palettes, the theme customizer's model, and live re-theming.

The whole UI is drawn from one palette - a ladder of neutral "slate" tones
(window, panel, surface, borders, dimmed and full text) plus a few accent
hues (blue, green, amber, red, sky, purple, pink). A *theme* is a remapping
of that palette, derived from a handful of role colours:

* Neutrals are blends of the theme's ``background``, ``surface`` and ``text``
  at the same ratios the built-in dark palette uses.
* Accent shades are lightened/darkened versions of each accent role. In light
  themes the shades flip around the base (a pale text tone becomes a deep
  one, a deep badge background becomes a pale tint), so the dark theme's
  dark-badge/bright-text pairs turn into readable tint/dark-text pairs.

How it reaches every widget: ``install()`` wraps ``QWidget.setStyleSheet``
once. The wrapper remembers each widget's palette-coloured sheet and installs
the remapped (and font-scaled) version. That single hook themes the global
sheet and every inline style in the app, and ``retheme()`` can re-apply all
of them for instant switching. Painted views (Gantt, charts) look colours up
through ``qcolor()``. The default Dark theme is the identity mapping, so with
it everything renders byte-for-byte as before.

The user's choice (a built-in theme or custom role colours, plus a font
scale) is stored in the ``appearance`` setting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget

APPEARANCE_KEY = "appearance"
RAW_QSS_PROPERTY = "_raw_qss"
MIN_FONT_SCALE, MAX_FONT_SCALE = 80, 160

# The role colours of the built-in dark palette (what the source code is written in)
ROLES: List[Tuple[str, str]] = [
    ("background", "Window background"),
    ("surface", "Cards & panels"),
    ("text", "Text"),
    ("primary", "Primary (buttons, selection)"),
    ("success", "Success / done"),
    ("warning", "Warning / amber"),
    ("danger", "Danger / overdue"),
    ("info", "Info / sky blue"),
    ("purple", "Purple accents"),
    ("pink", "Pink accents"),
]
DARK_ROLES: Dict[str, str] = {
    "background": "#0F172A", "surface": "#1E293B", "text": "#F8FAFC",
    "primary": "#3B82F6", "success": "#10B981", "warning": "#F59E0B", "danger": "#EF4444",
    "info": "#0EA5E9", "purple": "#8B5CF6", "pink": "#EC4899",
}

# Every neutral tone in the code, as a blend: (first stop, second stop, fraction toward the second)
_NEUTRALS: Dict[str, Tuple[str, str, float]] = {
    "#0F172A": ("background", "surface", 0.00),
    "#111827": ("background", "surface", 0.18),
    "#131C2E": ("background", "surface", 0.35),
    "#182234": ("background", "surface", 0.62),
    "#1E293B": ("background", "surface", 1.00),
    "#1F2937": ("background", "surface", 0.80),
    "#243046": ("surface", "text", 0.045),
    "#263244": ("surface", "text", 0.035),
    "#293548": ("surface", "text", 0.055),
    "#334155": ("surface", "text", 0.105),
    "#475569": ("surface", "text", 0.21),
    "#64748B": ("surface", "text", 0.345),
    "#94A3B8": ("surface", "text", 0.565),
    "#CBD5E1": ("surface", "text", 0.80),
    "#E2E8F0": ("surface", "text", 0.90),
    "#F1F5F9": ("surface", "text", 0.965),
    "#F8FAFC": ("surface", "text", 1.00),
    "#F4F4F4": ("surface", "text", 0.97),
}
# Accent tones: (role, shade level; 500 = the role colour itself). Light themes flip levels
# around 500 (a 400 text tone becomes 600, a 900 badge background becomes a pale 100 ...).
_ACCENTS: Dict[str, Tuple[str, int]] = {
    "#1D4ED8": ("primary", 700), "#2563EB": ("primary", 600), "#3B82F6": ("primary", 500),
    "#60A5FA": ("primary", 400), "#93C5FD": ("primary", 300), "#1E3A8A": ("primary", 900),
    "#0EA5E9": ("info", 500), "#38BDF8": ("info", 400), "#06B6D4": ("info", 520),
    "#059669": ("success", 600), "#10B981": ("success", 500), "#34D399": ("success", 400),
    "#A7F3D0": ("success", 200), "#064E3B": ("success", 900),
    "#D97706": ("warning", 600), "#F59E0B": ("warning", 500), "#FBBF24": ("warning", 400),
    "#92400E": ("warning", 800), "#422006": ("warning", 950),
    "#F97316": ("warning", 530), "#FB923C": ("warning", 430),
    "#DC2626": ("danger", 600), "#EF4444": ("danger", 500), "#F87171": ("danger", 400),
    "#FECACA": ("danger", 200), "#7F1D1D": ("danger", 900), "#450A0A": ("danger", 950),
    "#2A1D24": ("danger", 960),
    "#8B5CF6": ("purple", 500), "#A78BFA": ("purple", 400), "#C4B5FD": ("purple", 300),
    "#A855F7": ("purple", 480), "#6366F1": ("purple", 530),
    "#EC4899": ("pink", 500), "#F472B6": ("pink", 400),
}


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    c = hex_color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _hex(rgb: Tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02X}" for v in rgb)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def luminance(hex_color: str) -> float:
    r, g, b = _rgb(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two colours."""
    def channel(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    def rel(hex_color: str) -> float:
        r, g, b = _rgb(hex_color)
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    la, lb = sorted((rel(a), rel(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def shade(base: str, level: int) -> str:
    """A Tailwind-style shade of ``base`` (500 = base, lower = lighter, higher = darker)."""
    if level == 500:
        return base.upper()
    if level < 500:
        return mix(base, "#FFFFFF", min(0.95, (500 - level) / 470))
    return mix(base, "#000000", min(0.95, (level - 500) / 520))


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    colors: Dict[str, str]                 # role -> hex
    builtin: bool = True
    description: str = ""
    contrast: float = 1.0                  # < 1 spreads the neutral tones further apart

    @property
    def is_light(self) -> bool:
        return luminance(self.colors["text"]) < luminance(self.colors["background"])


def build_map(colors: Dict[str, str], contrast: float = 1.0) -> Dict[str, str]:
    """Source palette hex (upper case) -> themed hex. Tones whose role colours are unchanged
    from the dark palette keep their exact original value, so Dark is the identity."""
    roles = {**DARK_ROLES, **{k: v.upper() for k, v in colors.items() if v}}
    light = luminance(roles["text"]) < luminance(roles["background"])
    mapping: Dict[str, str] = {}
    for src, (a, b, t) in _NEUTRALS.items():
        if roles[a] == DARK_ROLES[a] and roles[b] == DARK_ROLES[b] and contrast == 1.0:
            continue
        if 0.0 < t < 1.0:
            t = t ** contrast
        mapping[src] = mix(roles[a], roles[b], t)
    for src, (role, level) in _ACCENTS.items():
        if roles[role] == DARK_ROLES[role] and not light:
            continue
        mapping[src] = shade(roles[role], 1000 - level if light else level)
    return {k: v for k, v in mapping.items() if k != v}


BUILTIN_THEMES: List[Theme] = [
    Theme("dark", "Dark", dict(DARK_ROLES), description="The classic deep-slate look (default)."),
    Theme("light", "Light", {
        "background": "#EEF1F6", "surface": "#FFFFFF", "text": "#111827",
        "primary": "#2563EB", "success": "#059669", "warning": "#B45309", "danger": "#DC2626",
        "info": "#0369A1", "purple": "#7C3AED", "pink": "#DB2777",
    }, description="A bright theme for daylight and projectors.", contrast=0.85),
    Theme("midnight", "Midnight", {
        "background": "#000000", "surface": "#101010", "text": "#E8E8E8",
        "primary": "#3B82F6", "success": "#10B981", "warning": "#F59E0B", "danger": "#EF4444",
        "info": "#0EA5E9", "purple": "#8B5CF6", "pink": "#EC4899",
    }, description="True black, easy on OLED screens."),
    Theme("nord", "Nord", {
        "background": "#2E3440", "surface": "#3B4252", "text": "#ECEFF4",
        "primary": "#81A1C1", "success": "#A3BE8C", "warning": "#EBCB8B", "danger": "#BF616A",
        "info": "#88C0D0", "purple": "#B48EAD", "pink": "#B48EAD",
    }, description="The calm arctic palette."),
    Theme("paper", "Paper", {
        "background": "#F3EEE3", "surface": "#FDFBF4", "text": "#3B3428",
        "primary": "#8C6D46", "success": "#5B7553", "warning": "#A8763E", "danger": "#A94442",
        "info": "#64748B", "purple": "#7A6174", "pink": "#A4656F",
    }, description="Warm sepia, like working on paper.", contrast=0.85),
    Theme("high_contrast", "High Contrast", {
        "background": "#000000", "surface": "#0A0A0A", "text": "#FFFFFF",
        "primary": "#1668CC", "success": "#2EE6A8", "warning": "#FFD633", "danger": "#FF5C5C",
        "info": "#33CCFF", "purple": "#C497FF", "pink": "#FF7ACC",
    }, description="Maximum contrast for low vision, with strong borders.", contrast=0.45),
]


def builtin_theme(key: str) -> Theme:
    return next((t for t in BUILTIN_THEMES if t.key == key), BUILTIN_THEMES[0])


# ---------------------------------------------------------------------------
# The engine (module-level state: one theme per process)
# ---------------------------------------------------------------------------
_current: Theme = BUILTIN_THEMES[0]
_map: Dict[str, str] = {}
_font_scale: int = 100
_hex_re = re.compile(r"#[0-9A-Fa-f]{6}\b")
_font_re = re.compile(r"(font-size\s*:\s*)(\d+)(px)", re.IGNORECASE)
_original_set_style_sheet: Optional[Callable] = None
_listeners: List[Callable[[], None]] = []


def current() -> Theme:
    return _current


def font_scale() -> int:
    return _font_scale


def color(source_hex: str) -> str:
    """The current theme's colour for a dark-palette hex."""
    return _map.get(source_hex.upper(), source_hex)


def qcolor(source_hex: str) -> QColor:
    return QColor(color(source_hex))


def apply(sheet: str) -> str:
    """A style sheet (or any text with palette colours) remapped and font-scaled."""
    if _map:
        sheet = _hex_re.sub(lambda m: _map.get(m.group(0).upper(), m.group(0)), sheet)
    if _font_scale != 100:
        sheet = _font_re.sub(lambda m: f"{m.group(1)}{max(7, round(int(m.group(2)) * _font_scale / 100))}{m.group(3)}",
                             sheet)
    return sheet


def install() -> None:
    """Route every ``setStyleSheet`` call through the theme (once per process)."""
    global _original_set_style_sheet
    if _original_set_style_sheet is not None:
        return
    _original_set_style_sheet = QWidget.setStyleSheet

    def themed_set_style_sheet(widget: QWidget, sheet: str) -> None:
        widget.setProperty(RAW_QSS_PROPERTY, sheet)
        _original_set_style_sheet(widget, apply(sheet))

    QWidget.setStyleSheet = themed_set_style_sheet


def add_listener(callback: Callable[[], None]) -> None:
    """``callback()`` runs after a theme change (painted views repaint from it)."""
    if callback not in _listeners:
        _listeners.append(callback)


def remove_listener(callback: Callable[[], None]) -> None:
    if callback in _listeners:
        _listeners.remove(callback)


def set_theme(theme: Theme, scale: Optional[int] = None) -> None:
    """Make ``theme`` current and restyle every live widget."""
    global _current, _map, _font_scale
    new_map = build_map(theme.colors, theme.contrast)
    new_scale = _font_scale if scale is None else max(MIN_FONT_SCALE, min(MAX_FONT_SCALE, int(scale)))
    unchanged = new_map == _map and new_scale == _font_scale
    _current, _map, _font_scale = theme, new_map, new_scale
    if not unchanged:  # e.g. every start-up with the default theme: nothing to restyle
        retheme()


def retheme() -> None:
    app = QApplication.instance()
    if app is None or _original_set_style_sheet is None:
        return
    for widget in app.allWidgets():
        raw = widget.property(RAW_QSS_PROPERTY)
        if raw is not None:
            _original_set_style_sheet(widget, apply(raw))
    for widget in app.allWidgets():
        widget.update()
    for callback in list(_listeners):
        callback()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
DEFAULT_APPEARANCE = {"theme": "dark", "custom": None, "font_scale": 100}


def load_appearance(service) -> Dict:
    s = dict(DEFAULT_APPEARANCE)
    s.update({k: v for k, v in (service.get_setting(APPEARANCE_KEY, {}) or {}).items() if k in DEFAULT_APPEARANCE})
    return s


def save_appearance(service, settings: Dict) -> Dict:
    s = load_appearance(service)
    s.update({k: v for k, v in settings.items() if k in DEFAULT_APPEARANCE})
    s["font_scale"] = max(MIN_FONT_SCALE, min(MAX_FONT_SCALE, int(s.get("font_scale") or 100)))
    service.set_setting(APPEARANCE_KEY, s)
    return s


def theme_from_appearance(settings: Dict) -> Theme:
    if settings.get("theme") == "custom" and settings.get("custom"):
        colors = {**DARK_ROLES, **{k: v for k, v in settings["custom"].items() if k in DARK_ROLES and v}}
        return Theme("custom", "Custom", colors, builtin=False, description="Your own colours.")
    return builtin_theme(settings.get("theme", "dark"))


def apply_appearance(service) -> Theme:
    """Install the hook, load the saved appearance and make it current."""
    install()
    settings = load_appearance(service)
    theme = theme_from_appearance(settings)
    set_theme(theme, settings.get("font_scale", 100))
    return theme


@dataclass
class _Check:
    pair: Tuple[str, str]
    ratio: float


def contrast_report(theme: Theme) -> List[Tuple[str, float, bool]]:
    """(what, ratio, ok) for the theme's key text/background pairs (WCAG AA normal text = 4.5)."""
    mapping = build_map(theme.colors, theme.contrast)

    def themed(h: str) -> str:
        return mapping.get(h, h)

    pairs = [
        ("Text on window", themed("#F8FAFC"), themed("#0F172A")),
        ("Text on cards", themed("#F8FAFC"), themed("#1E293B")),
        ("Secondary text on cards", themed("#94A3B8"), themed("#1E293B")),
        ("Dimmed text on window", themed("#64748B"), themed("#0F172A")),
        ("White on primary buttons", "#FFFFFF", theme.colors["primary"]),
    ]
    return [(label, round(contrast_ratio(a, b), 2), contrast_ratio(a, b) >= 4.5) for label, a, b in pairs]
