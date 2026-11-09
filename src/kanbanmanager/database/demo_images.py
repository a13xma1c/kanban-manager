"""
Procedurally Drawn PNG Images for the Demo Board

A tiny dependency-free raster canvas (rectangles, rounded rectangles, circles,
gradients) plus a PNG encoder. Used so the demo board can ship illustrative
image attachments without binary assets and without needing a Qt GUI context
(the demo is seeded before any QApplication exists).
"""
from __future__ import annotations

import struct
import zlib
from typing import Tuple

RGB = Tuple[int, int, int]


def hex_rgb(value: str) -> RGB:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


class Canvas:
    def __init__(self, width: int, height: int, background: str = "#FFFFFF"):
        self.width = width
        self.height = height
        px = bytes(hex_rgb(background))
        self.rows = [bytearray(px * width) for _ in range(height)]

    # -- primitives ---------------------------------------------------------
    def _span(self, y: int, x0: int, x1: int, color: RGB) -> None:
        if y < 0 or y >= self.height:
            return
        x0, x1 = max(0, x0), min(self.width, x1)
        if x1 > x0:
            self.rows[y][x0 * 3 : x1 * 3] = bytes(color) * (x1 - x0)

    def rect(self, x: int, y: int, w: int, h: int, color: str) -> "Canvas":
        c = hex_rgb(color)
        for yy in range(y, y + h):
            self._span(yy, x, x + w, c)
        return self

    def rounded_rect(self, x: int, y: int, w: int, h: int, r: int, color: str) -> "Canvas":
        c = hex_rgb(color)
        r = max(0, min(r, w // 2, h // 2))
        for yy in range(h):
            inset = 0
            if yy < r:
                dy = r - yy - 0.5
                inset = int(round(r - (r * r - dy * dy) ** 0.5))
            elif yy >= h - r:
                dy = yy - (h - r) + 0.5
                inset = int(round(r - max(0.0, r * r - dy * dy) ** 0.5))
            self._span(y + yy, x + inset, x + w - inset, c)
        return self

    def circle(self, cx: int, cy: int, r: int, color: str) -> "Canvas":
        c = hex_rgb(color)
        for dy in range(-r, r + 1):
            half = int((r * r - dy * dy) ** 0.5)
            self._span(cy + dy, cx - half, cx + half + 1, c)
        return self

    def vertical_gradient(self, top: str, bottom: str) -> "Canvas":
        a, b = hex_rgb(top), hex_rgb(bottom)
        for y in range(self.height):
            self._span(y, 0, self.width, _mix(a, b, y / max(1, self.height - 1)))
        return self

    def text_lines(self, x: int, y: int, widths, color: str = "#CBD5E1", height: int = 8, gap: int = 10) -> "Canvas":
        """Placeholder 'text' bars, as used in wireframes."""
        for i, w in enumerate(widths):
            self.rounded_rect(x, y + i * (height + gap), w, height, height // 2, color)
        return self

    # -- encoding -----------------------------------------------------------
    def to_png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(row) for row in self.rows)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# Demo artwork
# ---------------------------------------------------------------------------
def welcome_banner() -> bytes:
    c = Canvas(720, 200).vertical_gradient("#4F46E5", "#7C3AED")
    c.circle(660, 30, 70, "#6D5AF0").circle(70, 190, 50, "#6A3FE0")
    col_colors = ["#64748B", "#3B82F6", "#F59E0B", "#10B981"]
    for i, accent in enumerate(col_colors):
        x = 150 + i * 110
        c.rounded_rect(x, 36, 96, 132, 10, "#EEF2FF")
        c.rounded_rect(x + 8, 44, 80, 8, 4, accent)
        for j in range(3 - (i % 2)):
            c.rounded_rect(x + 8, 62 + j * 34, 80, 28, 6, "#FFFFFF")
            c.rounded_rect(x + 14, 70 + j * 34, 40, 5, 2, "#CBD5E1")
            c.rounded_rect(x + 14, 80 + j * 34, 58, 5, 2, "#E2E8F0")
    return c.to_png()


def onboarding_wireframe() -> bytes:
    c = Canvas(640, 380, "#F1F5F9")
    c.rounded_rect(20, 16, 600, 348, 12, "#FFFFFF")
    c.rect(20, 16, 600, 30, "#E2E8F0")
    for i, col in enumerate(("#F87171", "#FBBF24", "#34D399")):
        c.circle(40 + i * 18, 31, 5, col)
    c.rounded_rect(110, 24, 300, 14, 7, "#FFFFFF")
    c.rounded_rect(200, 80, 240, 18, 9, "#334155")
    c.text_lines(170, 112, [300, 260], "#CBD5E1")
    c.rounded_rect(250, 150, 140, 36, 10, "#3B82F6")
    c.rounded_rect(280, 164, 80, 8, 4, "#DBEAFE")
    for i in range(3):
        x = 92 + i * 156
        c.rounded_rect(x, 214, 140, 128, 10, "#E2E8F0")
        c.rounded_rect(x + 10, 224, 120, 64, 8, "#CBD5E1")  # skeleton preview placeholder
        c.text_lines(x + 10, 300, [90, 60], "#94A3B8", height=7, gap=8)
    return c.to_png()


def color_palette() -> bytes:
    c = Canvas(600, 140, "#0F172A")
    for i, col in enumerate(("#6366F1", "#3B82F6", "#10B981", "#F59E0B", "#EC4899")):
        x = 20 + i * 116
        c.rounded_rect(x, 20, 100, 72, 12, col)
        c.rounded_rect(x + 10, 104, 80, 10, 5, "#334155")
    return c.to_png()


def performance_chart() -> bytes:
    c = Canvas(640, 320, "#FFFFFF")
    for i in range(5):  # gridlines
        c.rect(60, 40 + i * 56, 560, 1, "#E2E8F0")
    c.rect(60, 40, 2, 226, "#94A3B8").rect(60, 264, 560, 2, "#94A3B8")
    values = [380, 410, 450, 470, 500, 560]  # Apr..Sep tasks completed
    colors = ["#93C5FD"] * 3 + ["#3B82F6"] * 3  # Q2 light, Q3 strong
    top = 600
    for i, (v, col) in enumerate(zip(values, colors)):
        h = int(v / top * 220)
        x = 90 + i * 88
        c.rounded_rect(x, 264 - h, 52, h, 6, col)
        c.rounded_rect(x + 6, 280, 40, 8, 4, "#CBD5E1")  # axis label placeholder
    for i, v in enumerate(values):  # trend markers
        c.circle(116 + i * 88, 264 - int(v / top * 220) - 14, 5, "#10B981")
    return c.to_png()


def crash_dialog() -> bytes:
    c = Canvas(480, 260, "#1E293B")
    c.rounded_rect(40, 30, 400, 200, 12, "#F8FAFC")
    c.rounded_rect(40, 30, 400, 40, 12, "#EF4444").rect(40, 56, 400, 14, "#EF4444")
    c.circle(66, 50, 10, "#FEE2E2").rect(64, 43, 4, 9, "#EF4444").rect(64, 54, 4, 3, "#EF4444")
    c.rounded_rect(86, 45, 160, 10, 5, "#FECACA")
    c.text_lines(64, 92, [320, 280, 300, 180], "#CBD5E1")
    c.rounded_rect(330, 184, 90, 30, 8, "#3B82F6")
    c.rounded_rect(230, 184, 90, 30, 8, "#E2E8F0")
    return c.to_png()
