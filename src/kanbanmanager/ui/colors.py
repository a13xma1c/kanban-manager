"""
Colour helpers for Qt style sheets.

Qt reads 8-digit hex colours as ``#AARRGGBB`` (alpha first), not the CSS
``#RRGGBBAA``. Appending an alpha suffix to a colour ("#8B5CF6" + "22") therefore
produces a completely different, opaque-ish colour (here a bright green). Use
``alpha()`` for a translucent version of a colour and ``tint()`` for a solid mix.
"""
from __future__ import annotations


def _rgb(color: str):
    c = (color or "#64748B").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (100, 116, 139)


def alpha(color: str, a: int) -> str:
    """``color`` with opacity ``a`` (0-255) as an rgba() style-sheet value."""
    r, g, b = _rgb(color)
    return f"rgba({r}, {g}, {b}, {max(0, min(255, int(a)))})"


def tint(color: str, amount: float = 0.22, base: str = "#111827") -> str:
    """Solid mix of ``color`` over ``base`` (``amount`` 0 = base, 1 = color)."""
    c, b = _rgb(color), _rgb(base)
    return "#" + "".join(f"{round(bv + (cv - bv) * amount):02X}" for cv, bv in zip(c, b))


def chip_style(color: str, bold: bool = True, radius: int = 4, padding: str = "2px 6px", size: int = 10) -> str:
    """Style for a small coloured pill: translucent fill, coloured text and border."""
    return (f"background-color: {alpha(color, 0x22)}; color: {color}; border: 1px solid {alpha(color, 0x66)}; "
            f"border-radius: {radius}px; padding: {padding}; font-size: {size}px;"
            + (" font-weight: 600;" if bold else ""))
