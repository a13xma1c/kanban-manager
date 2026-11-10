"""
Appearance dialog: pick a theme, build your own from role colours, and scale
the text size. Changes preview live; Cancel puts everything back.
"""
from __future__ import annotations

from functools import partial
from typing import Dict, Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.database.service import DatabaseService
from kanbanmanager.ui import theme as th


class ThemeSwatch(QWidget):
    """A clickable preview of one theme: window, a card with text lines, accent dots."""

    clicked = Signal(str)

    def __init__(self, theme: th.Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme
        self.selected = False
        self.setFixedSize(148, 112)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(f"Theme: {theme.label}")
        self.setToolTip(theme.description)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.theme.key)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit(self.theme.key)
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        mapping = th.build_map(self.theme.colors, self.theme.contrast)

        def c(src: str) -> QColor:
            return QColor(mapping.get(src, src))

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        border = th.qcolor("#3B82F6") if self.selected or self.hasFocus() else c("#334155")
        p.setPen(QPen(border, 2 if self.selected or self.hasFocus() else 1))
        p.setBrush(c("#0F172A"))
        p.drawRoundedRect(r, 8, 8)
        card = QRectF(r.left() + 10, r.top() + 10, r.width() - 20, 52)
        p.setPen(QPen(c("#334155"), 1))
        p.setBrush(c("#1E293B"))
        p.drawRoundedRect(card, 5, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(c("#F8FAFC"))
        p.drawRoundedRect(QRectF(card.left() + 8, card.top() + 9, card.width() * 0.55, 7), 2, 2)
        p.setBrush(c("#94A3B8"))
        p.drawRoundedRect(QRectF(card.left() + 8, card.top() + 24, card.width() * 0.75, 5), 2, 2)
        p.drawRoundedRect(QRectF(card.left() + 8, card.top() + 36, card.width() * 0.45, 5), 2, 2)
        x = r.left() + 10
        for role in ("primary", "success", "warning", "danger", "purple"):
            p.setBrush(QColor(self.theme.colors.get(role, "#888888")))
            p.drawEllipse(QRectF(x, card.bottom() + 8, 11, 11))
            x += 17
        p.setPen(c("#F8FAFC"))
        font = p.font()
        font.setPointSizeF(8.5)
        font.setBold(self.selected)
        p.setFont(font)
        p.drawText(QRectF(r.left(), r.bottom() - 20, r.width(), 18), Qt.AlignCenter, self.theme.label)
        p.end()


class ThemeDialog(QDialog):
    """Theme gallery + custom colours + text size, applied live."""

    def __init__(self, service: DatabaseService, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Appearance")
        self.setMinimumSize(700, 660)
        self._initial = th.load_appearance(service)
        self._custom: Dict[str, str] = dict(self._initial.get("custom") or th.current().colors)
        self.swatches: Dict[str, ThemeSwatch] = {}
        self.color_buttons: Dict[str, QPushButton] = {}
        self.selected_key = self._initial["theme"]

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        intro = QLabel("Pick a theme - it applies immediately so you can see it behind this window. "
                       "<b>Custom</b> lets you choose every colour yourself.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #94A3B8;")
        root.addWidget(intro)

        gallery_host = QWidget()
        root.addWidget(gallery_host)
        gallery = QGridLayout(gallery_host)
        gallery.setContentsMargins(0, 0, 0, 0)
        gallery.setSpacing(10)
        themes = list(th.BUILTIN_THEMES) + [self._custom_theme()]
        for i, theme in enumerate(themes):
            swatch = ThemeSwatch(theme)
            swatch.clicked.connect(self.choose)
            gallery.addWidget(swatch, i // 4, i % 4)
            self.swatches[theme.key] = swatch

        self.colors_host = QWidget()
        root.addWidget(self.colors_host)
        colors = QGridLayout(self.colors_host)
        colors.setContentsMargins(0, 0, 0, 0)
        colors.setHorizontalSpacing(14)
        colors.setVerticalSpacing(6)
        for i, (role, label) in enumerate(th.ROLES):
            lbl = QLabel(label)
            lbl.setStyleSheet("color: #CBD5E1;")
            colors.addWidget(lbl, i % 5, (i // 5) * 2)
            btn = QPushButton()
            btn.setFixedSize(84, 24)
            btn.setAccessibleName(f"Colour for {label}")
            btn.clicked.connect(partial(self.pick_color, role))
            colors.addWidget(btn, i % 5, (i // 5) * 2 + 1)
            self.color_buttons[role] = btn

        size_row = QHBoxLayout()
        root.addLayout(size_row)
        size_row.addWidget(QLabel("Text size:"))
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(th.MIN_FONT_SCALE, th.MAX_FONT_SCALE)
        self.scale_slider.setSingleStep(5)
        self.scale_slider.setPageStep(10)
        self.scale_slider.setValue(int(self._initial.get("font_scale", 100)))
        self.scale_slider.setAccessibleName("Text size scale")
        self.scale_slider.valueChanged.connect(self._on_scale)
        size_row.addWidget(self.scale_slider, 1)
        self.scale_lbl = QLabel("100%")
        self.scale_lbl.setMinimumWidth(44)
        size_row.addWidget(self.scale_lbl)

        self.contrast_lbl = QLabel("")
        self.contrast_lbl.setWordWrap(True)
        self.contrast_lbl.setTextFormat(Qt.RichText)
        self.contrast_lbl.setStyleSheet("color: #94A3B8; font-size: 11px;")
        root.addWidget(self.contrast_lbl)

        buttons = QHBoxLayout()
        root.addLayout(buttons)
        reset = QPushButton("Reset to Dark")
        reset.clicked.connect(self.reset)
        buttons.addWidget(reset)
        buttons.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("OK")
        ok.setObjectName("PrimaryButton")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)

        self.choose(self.selected_key, preview=False)
        self._on_scale(self.scale_slider.value(), preview=False)

    # ------------------------------------------------------------------
    def _custom_theme(self) -> th.Theme:
        colors = {**th.DARK_ROLES, **{k: v for k, v in self._custom.items() if k in th.DARK_ROLES and v}}
        return th.Theme("custom", "Custom", colors, builtin=False, description="Your own colours.")

    def theme_for(self, key: str) -> th.Theme:
        return self._custom_theme() if key == "custom" else th.builtin_theme(key)

    def choose(self, key: str, preview: bool = True) -> None:
        self.selected_key = key
        for k, swatch in self.swatches.items():
            swatch.set_selected(k == key)
        theme = self.theme_for(key)
        self.colors_host.setEnabled(key == "custom")
        for role, btn in self.color_buttons.items():
            value = theme.colors.get(role, "#888888")
            btn.setText(value)
            btn.setStyleSheet(f"background-color: {value}; color: {'#000000' if th.luminance(value) > 0.5 else '#FFFFFF'};"
                              f" border: 1px solid #475569; border-radius: 4px; font-size: 10px;")
        report = th.contrast_report(theme)
        parts = []
        for label, ratio, ok in report:
            icon = "✓" if ok else "⚠"
            parts.append(f"{icon} {label}: {ratio}")
        self.contrast_lbl.setText("Contrast (WCAG AA needs 4.5): " + " · ".join(parts))
        if preview:
            self.preview()

    def pick_color(self, role: str, _checked: bool = False) -> None:
        current = QColor(self._custom.get(role) or th.DARK_ROLES[role])
        color = QColorDialog.getColor(current, self, f"Colour: {dict(th.ROLES)[role]}")
        if not color.isValid():
            return
        self._custom[role] = color.name().upper()
        self.swatches["custom"].theme = self._custom_theme()
        self.swatches["custom"].update()
        self.choose("custom")

    def _on_scale(self, value: int, preview: bool = True) -> None:
        self.scale_lbl.setText(f"{value}%")
        if preview:
            self.preview()

    def preview(self) -> None:
        th.set_theme(self.theme_for(self.selected_key), self.scale_slider.value())

    def reset(self, _checked: bool = False) -> None:
        self.scale_slider.setValue(100)
        self.choose("dark")

    def values(self) -> Dict:
        return {"theme": self.selected_key,
                "custom": dict(self._custom) if self.selected_key == "custom" else self._initial.get("custom"),
                "font_scale": self.scale_slider.value()}

    def accept(self) -> None:
        th.save_appearance(self.service, self.values())
        self.preview()
        super().accept()

    def reject(self) -> None:
        th.set_theme(th.theme_from_appearance(self._initial), self._initial.get("font_scale", 100))
        super().reject()
