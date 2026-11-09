"""
Task Attachments Panel

Lists a task's attachments with thumbnails and a preview pane, and lets the
user add (button, drag & drop, paste), open, export, inline and remove files.

Changes are *staged* and only written by ``commit()`` when the task is saved,
matching the rest of the task dialog (Cancel discards everything). Staged
files get temporary ``new-<n>`` references so they can be shown inline in the
description before they have a database id.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import QFileInfo, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.config import IMAGE_EXTENSIONS
from kanbanmanager.database.attachments import AttachmentError, format_file_size
from kanbanmanager.database.service import DatabaseService

_THUMB = 48


def is_image_name(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


class _DropList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)

    @staticmethod
    def _files(event) -> List[str]:
        md = event.mimeData()
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile()] if md.hasUrls() else []

    def dragEnterEvent(self, event) -> None:
        if self._files(event):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._files(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        files = self._files(event)
        if files:
            event.acceptProposedAction()
            self.files_dropped.emit(files)
        else:
            super().dropEvent(event)


class AttachmentsPanel(QWidget):
    changed = Signal()                         # staged set changed (update tab title)
    insert_inline_requested = Signal(str, str)  # ref, alt text
    attachment_removed = Signal(str)           # ref of an attachment staged for removal

    def __init__(self, service: DatabaseService, task_id: Optional[int] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.service = service
        self.task_id = task_id
        self._existing: List[Dict[str, Any]] = []
        self._pending: List[Dict[str, Any]] = []
        self._removed: Set[int] = set()
        self._next_ref = 1
        self._tmpdir: Optional[Path] = None
        self._icons = QFileIconProvider()

        self._init_ui()
        self.reload()

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint = QLabel(
            "Drop files here, paste a screenshot into the description, or use <b>Add Files</b>. "
            f"Up to {format_file_size(self.service.db.attachment_store.max_bytes)} per file. "
            "Changes are saved with the task."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #94A3B8; font-size: 11px;")
        layout.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.list = _DropList()
        self.list.setIconSize(QSize(_THUMB, _THUMB))
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.files_dropped.connect(self.add_files)
        self.list.currentItemChanged.connect(lambda *_: self._update_preview())
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.itemDoubleClicked.connect(lambda _item: self.open_selected())
        body.addWidget(self.list, 3)

        preview = QFrame()
        preview.setObjectName("AttachmentPreview")
        preview.setStyleSheet(
            "#AttachmentPreview { background-color: #0F172A; border: 1px solid #334155; border-radius: 8px; }"
        )
        pv = QVBoxLayout(preview)
        pv.setContentsMargins(10, 10, 10, 10)
        self.preview_image = QLabel("Select an attachment to preview it")
        self.preview_image.setAlignment(Qt.AlignCenter)
        self.preview_image.setMinimumSize(220, 150)
        self.preview_image.setStyleSheet("color: #64748B; background: transparent; border: none;")
        self.preview_image.setWordWrap(True)
        pv.addWidget(self.preview_image, 1)
        self.preview_info = QLabel("")
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextFormat(Qt.RichText)
        self.preview_info.setStyleSheet("color: #94A3B8; font-size: 11px; background: transparent; border: none;")
        pv.addWidget(self.preview_info)
        body.addWidget(preview, 2)
        layout.addLayout(body, 1)

        btns = QHBoxLayout()
        self.add_btn = QPushButton("📎 Add Files...")
        self.add_btn.clicked.connect(self._browse)
        btns.addWidget(self.add_btn)
        self.inline_btn = QPushButton("🖼 Insert in Description")
        self.inline_btn.setToolTip("Show the selected image inline in the task description")
        self.inline_btn.clicked.connect(self._insert_inline)
        btns.addWidget(self.inline_btn)
        btns.addStretch()
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(self.open_selected)
        btns.addWidget(self.open_btn)
        self.save_btn = QPushButton("Save As...")
        self.save_btn.clicked.connect(self._save_as)
        btns.addWidget(self.save_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("DangerButton")
        self.remove_btn.clicked.connect(self.remove_selected)
        btns.addWidget(self.remove_btn)
        layout.addLayout(btns)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    def reload(self) -> None:
        self._existing = self.service.get_attachments(self.task_id) if self.task_id else []
        self._refresh_list()

    def entries(self) -> List[Dict[str, Any]]:
        """Attachments as they will be after saving (existing minus removed, plus pending)."""
        kept = [dict(a, ref=str(a["id"]), pending=False) for a in self._existing if a["id"] not in self._removed]
        return kept + [dict(p, pending=True) for p in self._pending]

    def count(self) -> int:
        return len(self.entries())

    def has_changes(self) -> bool:
        return bool(self._pending or self._removed)

    def resolve(self, ref: str) -> Optional[str]:
        """Map an ``attachment:<ref>`` reference to a local file path (for inline images)."""
        for p in self._pending:
            if p["ref"] == ref:
                return p["path"]
        for a in self._existing:
            if str(a["id"]) == ref:
                return a["path"] if a.get("file_exists") else None
        if ref.isdigit():  # e.g. an image of another task pasted in; still try the store
            path = self.service.get_attachment_path(int(ref))
            return str(path) if path else None
        return None

    def add_files(self, paths: List[str]) -> List[Tuple[str, str]]:
        """Stage files; returns [(ref, filename)] for the ones accepted."""
        added: List[Tuple[str, str]] = []
        problems: List[str] = []
        limit = self.service.db.attachment_store.max_bytes
        for raw in paths:
            p = Path(raw)
            if not p.is_file():
                problems.append(f"{p.name or raw}: not a file")
                continue
            size = p.stat().st_size
            if limit and size > limit:
                problems.append(f"{p.name}: {format_file_size(size)} exceeds the {format_file_size(limit)} limit")
                continue
            added.append(self._stage(p, p.name, size))
        if problems:
            QMessageBox.warning(self, "Some files were not attached", "\n".join(problems))
        if added:
            self._refresh_list(select_ref=added[-1][0])
            self.changed.emit()
        return added

    def stage_image(self, image: QImage, basename: str = "pasted_image") -> Optional[Tuple[str, str]]:
        """Stage a clipboard image as a PNG file."""
        if image.isNull():
            return None
        if self._tmpdir is None:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="kanban_paste_"))
        name = f"{basename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._next_ref}.png"
        path = self._tmpdir / name
        if not image.save(str(path), "PNG"):
            QMessageBox.warning(self, "Paste failed", "Could not save the pasted image.")
            return None
        result = self.add_files([str(path)])
        return result[0] if result else None

    def _stage(self, path: Path, filename: str, size: int) -> Tuple[str, str]:
        ref = f"new-{self._next_ref}"
        self._next_ref += 1
        self._pending.append(
            {
                "ref": ref,
                "path": str(path),
                "filename": filename,
                "size_bytes": size,
                "is_image": is_image_name(filename),
                "uploaded_by": self.service.actor,
                "created_at": None,
                "file_exists": True,
            }
        )
        return ref, filename

    def commit(self, task_id: int) -> Tuple[Dict[str, str], List[str]]:
        """Apply staged removals and additions. Returns ({new-ref: id}, [error messages])."""
        mapping: Dict[str, str] = {}
        errors: List[str] = []
        for att_id in sorted(self._removed):
            self.service.delete_attachment(att_id)
        for p in self._pending:
            try:
                info = self.service.add_attachment(task_id, p["path"], p["filename"])
                mapping[p["ref"]] = str(info["id"])
            except (AttachmentError, OSError) as exc:
                errors.append(f"{p['filename']}: {exc}")
        self.task_id = task_id
        self._pending.clear()
        self._removed.clear()
        self.reload()
        return mapping, errors

    def cleanup(self) -> None:
        if self._tmpdir and self._tmpdir.exists():
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self._tmpdir = None

    # ------------------------------------------------------------------
    # List / preview
    # ------------------------------------------------------------------
    def _icon_for(self, entry: Dict[str, Any]) -> QIcon:
        if entry.get("is_image") and entry.get("file_exists"):
            pix = QPixmap(entry["path"])
            if not pix.isNull():
                return QIcon(pix.scaled(_THUMB, _THUMB, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        return self._icons.icon(QFileInfo(entry.get("filename", "")))

    def _refresh_list(self, select_ref: Optional[str] = None) -> None:
        current = select_ref or self._selected_ref()
        entries = self.entries()
        if current is None and entries:
            current = entries[0]["ref"]  # preview something useful straight away
        self.list.clear()
        for e in entries:
            meta = format_file_size(e.get("size_bytes", 0))
            if e["pending"]:
                meta += "  ·  new - saved with the task"
            elif not e.get("file_exists"):
                meta += "  ·  ⚠ file missing"
            elif e.get("uploaded_by"):
                meta += f"  ·  {e['uploaded_by']}"
            item = QListWidgetItem(self._icon_for(e), f"{e['filename']}\n{meta}")
            item.setData(Qt.UserRole, e["ref"])
            item.setToolTip(e["filename"])
            self.list.addItem(item)
            if e["ref"] == current:
                self.list.setCurrentItem(item)
        self._update_preview()
        self._update_buttons()

    def _entry(self, ref: Optional[str]) -> Optional[Dict[str, Any]]:
        return next((e for e in self.entries() if e["ref"] == ref), None) if ref else None

    def _selected_ref(self) -> Optional[str]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _selected_entries(self) -> List[Dict[str, Any]]:
        refs = [i.data(Qt.UserRole) for i in self.list.selectedItems()]
        return [e for e in self.entries() if e["ref"] in refs]

    def _update_preview(self) -> None:
        e = self._entry(self._selected_ref())
        if not e:
            self.preview_image.setPixmap(QPixmap())
            self.preview_image.setText("Select an attachment to preview it" if self.count() else "No attachments yet")
            self.preview_info.setText("")
            return
        shown = False
        if e.get("is_image") and e.get("file_exists"):
            pix = QPixmap(e["path"])
            if not pix.isNull():
                self.preview_image.setPixmap(
                    pix.scaled(self.preview_image.size().expandedTo(QSize(220, 150)), Qt.KeepAspectRatio,
                               Qt.SmoothTransformation)
                )
                shown = True
        if not shown:
            pix = self._icon_for(e).pixmap(64, 64)
            if pix.isNull():  # no icon theme available (e.g. minimal Linux desktops)
                ext = Path(e["filename"]).suffix.lstrip(".").upper() or "FILE"
                self.preview_image.setPixmap(QPixmap())
                self.preview_image.setText(f"📄\n{ext}")
            else:
                self.preview_image.setPixmap(pix)
        added = ""
        if e.get("created_at"):
            try:
                added = datetime.fromisoformat(e["created_at"]).strftime("%b %d, %Y")
            except ValueError:
                pass
        parts = [f"<b style='color:#F8FAFC'>{_esc(e['filename'])}</b>", format_file_size(e.get("size_bytes", 0))]
        if e["pending"]:
            parts.append("Will be uploaded when you save the task")
        else:
            who = e.get("uploaded_by") or "Unknown"
            parts.append(f"Added by {_esc(who)}" + (f" on {added}" if added else ""))
            if not e.get("file_exists"):
                parts.append("<span style='color:#EF4444'>The stored file is missing.</span>")
        self.preview_info.setText("<br>".join(parts))

    def _update_buttons(self) -> None:
        sel = self._selected_entries()
        one = len(sel) == 1
        available = one and bool(sel[0].get("file_exists"))
        self.open_btn.setEnabled(available)
        self.save_btn.setEnabled(available)
        self.inline_btn.setEnabled(available and bool(sel[0].get("is_image")))
        self.remove_btn.setEnabled(bool(sel))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach Files")
        if paths:
            self.add_files(paths)

    def _insert_inline(self) -> None:
        sel = self._selected_entries()
        if len(sel) == 1 and sel[0].get("is_image"):
            self.insert_inline_requested.emit(sel[0]["ref"], Path(sel[0]["filename"]).stem)

    def open_selected(self) -> None:
        sel = self._selected_entries()
        if len(sel) == 1 and sel[0].get("file_exists"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(sel[0]["path"]))

    def _save_as(self) -> None:
        sel = self._selected_entries()
        if len(sel) != 1 or not sel[0].get("file_exists"):
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save Attachment As", sel[0]["filename"])
        if dest:
            try:
                shutil.copyfile(sel[0]["path"], dest)
            except OSError as exc:
                QMessageBox.critical(self, "Save failed", str(exc))

    def remove_selected(self, confirm: bool = True) -> None:
        sel = self._selected_entries()
        if not sel:
            return
        if confirm:
            names = "\n".join(f"• {e['filename']}" for e in sel)
            reply = QMessageBox.question(
                self,
                "Remove Attachments",
                f"Remove these attachments when the task is saved?\n\n{names}\n\n"
                "Inline images of them are removed from the description too.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        for e in sel:
            if e["pending"]:
                self._pending = [p for p in self._pending if p["ref"] != e["ref"]]
            else:
                self._removed.add(int(e["ref"]))
            self.attachment_removed.emit(e["ref"])
        self._refresh_list()
        self.changed.emit()


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
