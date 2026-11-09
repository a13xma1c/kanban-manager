"""
Markdown & WYSIWYG Description Editor

``MarkdownEditor`` edits Markdown text in two interchangeable modes:

* **Visual** - a WYSIWYG QTextEdit. Formatting is applied with a toolbar or
  shortcuts and converted back to Markdown with QTextDocument.toMarkdown().
* **Markdown** - the raw source in a QPlainTextEdit with syntax highlighting;
  toolbar actions insert the equivalent Markdown syntax.

Markdown is always the stored format. Text that was not edited is returned
byte-for-byte as loaded, so merely opening and saving a task never rewrites
its description (and never adds a spurious audit-trail entry).

Inline images use ``attachment:<ref>`` URLs that are resolved to files through
a caller-supplied resolver. Pasted images and dropped files are surfaced as
signals so the owner can stage them as task attachments.
"""
from __future__ import annotations

import re
from typing import Callable, List, Optional

from PySide6.QtCore import QPointF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QPalette,
    QFont,
    QFontDatabase,
    QFontInfo,
    QImage,
    QKeySequence,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextImageFormat,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from kanbanmanager.core.markdown_utils import (
    attachment_url,
    markdown_to_plain,
    parse_attachment_url,
    tidy_qt_markdown,
)

Resolver = Callable[[str], Optional[str]]

_HEADING_SIZE = {1: 3, 2: 2, 3: 1}  # QTextFormat.FontSizeAdjustment used by Qt's Markdown importer
_MAX_IMAGE_WIDTH = 560


def _fixed_font_families() -> List[str]:
    return QFontDatabase.systemFont(QFontDatabase.FixedFont).families() or ["monospace"]


def _is_code_format(fmt: QTextCharFormat) -> bool:
    return fmt.fontFixedPitch() or QFontInfo(fmt.font()).fixedPitch()


def _placeholder_image(text: str) -> QImage:
    img = QImage(240, 60, QImage.Format_ARGB32)
    img.fill(QColor("#334155"))
    p = QPainter(img)
    p.setPen(QColor("#CBD5E1"))
    p.drawText(img.rect(), Qt.AlignCenter, text)
    p.end()
    return img


class LinkDialog(QDialog):
    """Small prompt for link text + URL."""

    def __init__(self, text: str = "", url: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Insert Link")
        self.setMinimumWidth(380)
        form = QFormLayout(self)
        self.text_input = QLineEdit(text)
        self.text_input.setPlaceholderText("Text to display")
        self.url_input = QLineEdit(url)
        self.url_input.setPlaceholderText("https://")
        form.addRow("Text:", self.text_input)
        form.addRow("URL:", self.url_input)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        (self.url_input if text else self.text_input).setFocus()

    def values(self) -> tuple[str, str]:
        url = self.url_input.text().strip()
        return (self.text_input.text().strip() or url), url


class RichTextEdit(QTextEdit):
    """WYSIWYG surface: resolves attachment images, toggles task-list boxes, and
    reports pasted images / dropped files instead of embedding them."""

    image_pasted = Signal(QImage)
    files_dropped = Signal(list)  # list[str] local file paths

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.resolver: Optional[Resolver] = None
        self.setAcceptRichText(True)

    # Images ---------------------------------------------------------------
    def loadResource(self, rtype: int, url: QUrl):
        if rtype == QTextDocument.ImageResource:
            ref = parse_attachment_url(url.toString())
            if ref is not None:
                path = self.resolver(ref) if self.resolver else None
                img = QImage(path) if path else QImage()
                if img.isNull():
                    return _placeholder_image("Image unavailable")
                max_w = min(_MAX_IMAGE_WIDTH, max(200, self.viewport().width() - 40))
                if img.width() > max_w:
                    img = img.scaledToWidth(max_w, Qt.SmoothTransformation)
                return img
        return super().loadResource(rtype, url)

    def reload_images(self) -> None:
        """Re-resolve inline images (e.g. after attachments were staged or saved)."""
        md = self.document().toMarkdown()
        modified = self.document().isModified()
        self.document().setMarkdown(md)
        self.document().setModified(modified)

    # Paste / drop ---------------------------------------------------------
    @staticmethod
    def _local_files(source) -> List[str]:
        if not source.hasUrls():
            return []
        return [u.toLocalFile() for u in source.urls() if u.isLocalFile() and u.toLocalFile()]

    def canInsertFromMimeData(self, source) -> bool:
        return bool(self._local_files(source)) or source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        files = self._local_files(source)
        if files:
            self.files_dropped.emit(files)
            return
        if source.hasImage():
            img = QImage(source.imageData())
            if not img.isNull():
                self.image_pasted.emit(img)
                return
        super().insertFromMimeData(source)

    # Task-list checkboxes -------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            pos = event.position()
            doc_pos = QPointF(
                pos.x() + self.horizontalScrollBar().value(), pos.y() + self.verticalScrollBar().value()
            )
            block = self.document().documentLayout().blockWithMarkerAt(doc_pos)
            if block.isValid():
                fmt = block.blockFormat()
                marker = fmt.marker()
                if marker != QTextBlockFormat.MarkerType.NoMarker:
                    fmt.setMarker(
                        QTextBlockFormat.MarkerType.Unchecked
                        if marker == QTextBlockFormat.MarkerType.Checked
                        else QTextBlockFormat.MarkerType.Checked
                    )
                    QTextCursor(block).setBlockFormat(fmt)
                    event.accept()
                    return
        super().mousePressEvent(event)


class MarkdownHighlighter(QSyntaxHighlighter):
    """Lightweight Markdown syntax highlighting for the source view."""

    def __init__(self, document: QTextDocument):
        super().__init__(document)

        def fmt(color: str, bold: bool = False, italic: bool = False, mono: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            if italic:
                f.setFontItalic(True)
            if mono:
                f.setFontFamilies(_fixed_font_families())
            return f

        self._rules = [
            (re.compile(r"^\s{0,3}#{1,6}\s.*$"), fmt("#93C5FD", bold=True)),
            (re.compile(r"^\s*>.*$"), fmt("#94A3B8", italic=True)),
            (re.compile(r"^\s*([-*+]|\d+[.)])\s(\[[ xX]\]\s)?"), fmt("#F59E0B", bold=True)),
            (re.compile(r"(\*\*|__)(?=\S).+?(?<=\S)\1"), fmt("#F8FAFC", bold=True)),
            (re.compile(r"(?<![*\w])[*_](?=\S)[^*_]+?(?<=\S)[*_](?![*\w])"), fmt("#E2E8F0", italic=True)),
            (re.compile(r"~~.+?~~"), fmt("#64748B")),
            (re.compile(r"`[^`]+`"), fmt("#F472B6", mono=True)),
            (re.compile(r"!?\[[^\]]*\]\([^)]*\)"), fmt("#38BDF8")),
        ]
        self._fence = fmt("#F472B6", mono=True)

    def highlightBlock(self, text: str) -> None:
        in_fence = self.previousBlockState() == 1
        is_fence_line = bool(re.match(r"^\s{0,3}(```|~~~)", text))
        if in_fence or is_fence_line:
            self.setFormat(0, len(text), self._fence)
            self.setCurrentBlockState(0 if (in_fence and is_fence_line) else 1)
            return
        self.setCurrentBlockState(0)
        for pattern, style in self._rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), style)


class MarkdownEditor(QWidget):
    """Dual-mode (Visual / Markdown) editor whose value is Markdown text."""

    VISUAL = "visual"
    MARKDOWN = "markdown"

    image_requested = Signal()          # toolbar "insert image" clicked
    image_pasted = Signal(QImage)       # clipboard image pasted
    files_dropped = Signal(list)        # local files dropped / pasted
    mode_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, placeholder: str = ""):
        super().__init__(parent)
        self._mode = self.VISUAL
        self._original = ""
        self._md = ""
        self._rich_baseline = ""
        self._buttons: dict[str, QToolButton] = {}

        self._init_ui(placeholder)
        self.set_markdown("")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _init_ui(self, placeholder: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QFrame()
        bar.setObjectName("EditorToolbar")
        bar.setStyleSheet(
            "#EditorToolbar { background-color: #1E293B; border: 1px solid #334155; border-radius: 6px; }"
            "QToolButton { background: transparent; color: #CBD5E1; border: none; border-radius: 4px;"
            " padding: 3px 6px; font-size: 12px; min-width: 18px; }"
            "QToolButton:hover { background-color: #334155; color: #F8FAFC; }"
            "QToolButton:checked { background-color: #3B82F6; color: #FFFFFF; }"
        )
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(1)

        def button(key: str, text: str, tip: str, slot, checkable: bool = False, style: str = "") -> QToolButton:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setFocusPolicy(Qt.NoFocus)
            if style:
                b.setStyleSheet(style)
            b.clicked.connect(slot)
            self._buttons[key] = b
            row.addWidget(b)
            return b

        def sep() -> None:
            line = QFrame()
            line.setFrameShape(QFrame.VLine)
            line.setStyleSheet("color: #334155;")
            row.addWidget(line)

        button("bold", "B", "Bold (Ctrl+B)", self.toggle_bold, True, "font-weight: 800;")
        button("italic", "I", "Italic (Ctrl+I)", self.toggle_italic, True, "font-style: italic;")
        button("strike", "S", "Strikethrough", self.toggle_strike, True, "text-decoration: line-through;")
        button("code", "</>", "Inline code (Ctrl+E)", self.toggle_code, True)
        sep()
        for level in (1, 2, 3):
            button(f"h{level}", f"H{level}", f"Heading {level}", lambda _=False, l=level: self.toggle_heading(l), True)
        sep()
        button("bullets", "•≡", "Bulleted list", self.toggle_bullets, True)
        button("numbers", "1≡", "Numbered list", self.toggle_numbers, True)
        button("tasks", "☑", "Task list (click a box to check it)", self.toggle_tasks, True)
        button("quote", "❝", "Quote", self.toggle_quote, True)
        button("codeblock", "{ }", "Code block", self.toggle_code_block, True)
        sep()
        button("link", "🔗", "Insert link (Ctrl+K)", self.insert_link)
        button("image", "🖼", "Insert image (also paste or drop one)", self.image_requested.emit)
        row.addStretch()

        mode_style = (
            "QToolButton { border: 1px solid #334155; padding: 3px 10px; }"
            "QToolButton:checked { background-color: #334155; color: #F8FAFC; border-color: #475569; }"
        )
        self.visual_btn = QToolButton()
        self.visual_btn.setText("Visual")
        self.markdown_btn = QToolButton()
        self.markdown_btn.setText("Markdown")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for b, mode, tip in (
            (self.visual_btn, self.VISUAL, "Edit formatted text (WYSIWYG)"),
            (self.markdown_btn, self.MARKDOWN, "Edit the raw Markdown source"),
        ):
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.setStyleSheet(mode_style)
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            self._mode_group.addButton(b)
            row.addWidget(b)
        self.visual_btn.setChecked(True)

        layout.addWidget(bar)

        self.stack = QStackedWidget()
        self.rich = RichTextEdit()
        self.rich.setPlaceholderText(placeholder)
        self.rich.document().setDocumentMargin(10)
        pal = self.rich.palette()
        pal.setColor(QPalette.Link, QColor("#60A5FA"))  # readable on the dark theme
        self.rich.setPalette(pal)
        self.rich.image_pasted.connect(self.image_pasted.emit)
        self.rich.files_dropped.connect(self.files_dropped.emit)
        self.rich.currentCharFormatChanged.connect(lambda _f: self._update_toolbar_state())
        self.rich.cursorPositionChanged.connect(self._update_toolbar_state)

        self.source = QPlainTextEdit()
        self.source.setPlaceholderText(placeholder or "Write Markdown here...")
        mono = QFont(_fixed_font_families()[0])
        mono.setStyleHint(QFont.Monospace)
        self.source.setFont(mono)
        self.source.setTabChangesFocus(False)
        self._highlighter = MarkdownHighlighter(self.source.document())

        self.stack.addWidget(self.rich)
        self.stack.addWidget(self.source)
        layout.addWidget(self.stack, 1)

        for seq, slot in (
            ("Ctrl+B", self.toggle_bold),
            ("Ctrl+I", self.toggle_italic),
            ("Ctrl+E", self.toggle_code),
            ("Ctrl+K", self.insert_link),
        ):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    # ------------------------------------------------------------------
    # Value & mode
    # ------------------------------------------------------------------
    def set_resource_resolver(self, resolver: Resolver) -> None:
        self.rich.resolver = resolver

    def mode(self) -> str:
        return self._mode

    def set_markdown(self, markdown: str) -> None:
        """Load new content; it becomes the unmodified baseline."""
        self._original = markdown or ""
        self._md = self._original
        self._load_active_view()

    def replace_markdown(self, markdown: str) -> None:
        """Replace content as an edit (the result counts as modified)."""
        self._sync_from_active_view()
        self._md = markdown or ""
        self._load_active_view()

    def markdown(self) -> str:
        self._sync_from_active_view()
        return self._md

    def is_modified(self) -> bool:
        return self.markdown() != self._original

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        anchor = self._cursor_anchor()
        self._sync_from_active_view()
        self._mode = mode
        self._load_active_view()
        self._restore_cursor(anchor)
        self.visual_btn.setChecked(mode == self.VISUAL)
        self.markdown_btn.setChecked(mode == self.MARKDOWN)
        for key in ("bold", "italic", "strike", "code", "h1", "h2", "h3", "bullets", "numbers", "tasks", "quote",
                    "codeblock"):
            self._buttons[key].setCheckable(mode == self.VISUAL)
        self._update_toolbar_state()
        self.mode_changed.emit(mode)

    def reload_images(self) -> None:
        if self._mode == self.VISUAL:
            self.rich.reload_images()

    def _load_active_view(self) -> None:
        if self._mode == self.VISUAL:
            doc = self.rich.document()
            doc.setUndoRedoEnabled(False)  # loading (and polishing) is not an undoable edit
            doc.setMarkdown(self._md)
            self._polish_document(doc)
            doc.setUndoRedoEnabled(True)
            self._rich_baseline = tidy_qt_markdown(doc.toMarkdown())
            doc.setModified(False)
            self.rich.moveCursor(QTextCursor.Start)
            self.stack.setCurrentWidget(self.rich)
        else:
            self.source.setPlainText(self._md)
            self.source.document().setModified(False)
            self.source.moveCursor(QTextCursor.Start)
            self.stack.setCurrentWidget(self.source)

    @staticmethod
    def _polish_document(doc: QTextDocument) -> None:
        """Add vertical rhythm between blocks. Margins are presentation only and are
        not written by toMarkdown(), so the Markdown is unaffected."""
        block = doc.begin()
        cur = QTextCursor(doc)
        while block.isValid():
            bf = block.blockFormat()
            in_list = block.textList() is not None
            is_code = bf.hasProperty(QTextFormat.BlockCodeFence)
            nxt = block.next()
            next_same_kind = nxt.isValid() and (
                (in_list and nxt.textList() is not None)
                or (is_code and nxt.blockFormat().hasProperty(QTextFormat.BlockCodeFence))
            )
            if bf.headingLevel():
                bf.setTopMargin(10 if block != doc.begin() else 0)
                bf.setBottomMargin(4)
            else:
                bf.setBottomMargin(0 if next_same_kind else 8)
            cur.setPosition(block.position())
            cur.setBlockFormat(bf)
            block = nxt

    # Cursor carry-over between modes ------------------------------------
    def _active_editor(self):
        return self.rich if self._mode == self.VISUAL else self.source

    def _block_plain(self, block: QTextBlock) -> str:
        text = block.text()
        return (markdown_to_plain(text) if self._mode == self.MARKDOWN else text).strip()

    def _cursor_anchor(self) -> tuple[str, bool, float]:
        """(plain text of the cursor's block, cursor at end of block?, relative position)."""
        editor = self._active_editor()
        cur = editor.textCursor()
        block = cur.block()
        at_end = cur.positionInBlock() >= len(block.text())
        fraction = cur.position() / max(1, editor.document().characterCount() - 1)
        return self._block_plain(block), at_end, fraction

    def _restore_cursor(self, anchor: tuple[str, bool, float]) -> None:
        """Put the cursor on the equivalent block in the newly shown view."""
        key, at_end, fraction = anchor
        editor = self._active_editor()
        doc = editor.document()
        target = None
        if key:
            block = doc.begin()
            partial = None
            while block.isValid():
                plain = self._block_plain(block)
                if plain == key:
                    target = block
                    break
                if partial is None and len(key) > 3 and plain and (key in plain or plain in key):
                    partial = block
                block = block.next()
            target = target or partial
        cur = QTextCursor(doc)
        if target is not None:
            cur.setPosition(target.position() + (len(target.text()) if at_end else 0))
        else:
            cur.setPosition(int(fraction * max(0, doc.characterCount() - 1)))
            cur.movePosition(QTextCursor.EndOfBlock if at_end else QTextCursor.StartOfBlock)
        editor.setTextCursor(cur)
        editor.ensureCursorVisible()

    def _sync_from_active_view(self) -> None:
        if self._mode == self.VISUAL:
            doc = self.rich.document()
            if doc.isModified():
                out = tidy_qt_markdown(doc.toMarkdown())
                if out != self._rich_baseline:  # an edit that was undone is not a change
                    self._md = out
                    self._rich_baseline = out
                doc.setModified(False)
        else:
            doc = self.source.document()
            if doc.isModified():
                self._md = self.source.toPlainText()
                doc.setModified(False)

    # ------------------------------------------------------------------
    # Insertions (both modes)
    # ------------------------------------------------------------------
    def insert_image(self, ref: str, alt: str = "") -> None:
        alt = (alt or "image").replace("[", "(").replace("]", ")")
        if self._mode == self.VISUAL:
            cur = self.rich.textCursor()
            cur.beginEditBlock()
            if cur.block().text().strip():
                cur.movePosition(QTextCursor.EndOfBlock)
                cur.insertBlock()
            fmt = QTextImageFormat()
            fmt.setName(attachment_url(ref))
            fmt.setProperty(QTextFormat.ImageAltText, alt)
            cur.insertImage(fmt)
            cur.insertBlock()
            cur.endEditBlock()
            self.rich.setTextCursor(cur)
        else:
            cur = self.source.textCursor()
            prefix = "\n" if cur.positionInBlock() > 0 else ""
            cur.insertText(f"{prefix}![{alt}]({attachment_url(ref)})\n")
            self.source.setTextCursor(cur)

    def insert_link(self) -> None:
        if self._mode == self.VISUAL:
            cur = self.rich.textCursor()
            dlg = LinkDialog(cur.selectedText(), cur.charFormat().anchorHref(), self)
            if not dlg.exec():
                return
            text, url = dlg.values()
            if not url:
                return
            fmt = QTextCharFormat()
            fmt.setAnchor(True)
            fmt.setAnchorHref(url)
            fmt.setForeground(QColor("#60A5FA"))
            fmt.setFontUnderline(True)
            cur.insertText(text, fmt)
            self.rich.setCurrentCharFormat(QTextCharFormat())
            self.rich.setTextCursor(cur)
        else:
            cur = self.source.textCursor()
            dlg = LinkDialog(cur.selectedText(), "", self)
            if not dlg.exec():
                return
            text, url = dlg.values()
            if url:
                cur.insertText(f"[{text}]({url})")

    # ------------------------------------------------------------------
    # Inline formatting
    # ------------------------------------------------------------------
    def toggle_bold(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._wrap_source("**", "**", "bold text")
        f = QTextCharFormat()
        f.setFontWeight(QFont.Normal if self.rich.fontWeight() > QFont.Normal else QFont.Bold)
        self._merge_char(f)

    def toggle_italic(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._wrap_source("*", "*", "italic text")
        f = QTextCharFormat()
        f.setFontItalic(not self.rich.fontItalic())
        self._merge_char(f)

    def toggle_strike(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._wrap_source("~~", "~~", "struck text")
        f = QTextCharFormat()
        f.setFontStrikeOut(not self.rich.currentCharFormat().fontStrikeOut())
        self._merge_char(f)

    def toggle_code(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._wrap_source("`", "`", "code")
        f = QTextCharFormat()
        if _is_code_format(self.rich.currentCharFormat()):
            f.setFontFixedPitch(False)
            f.setFontFamilies(self.rich.document().defaultFont().families())
        else:
            f.setFontFixedPitch(True)
            f.setFontFamilies(_fixed_font_families())
        self._merge_char(f)

    def _merge_char(self, fmt: QTextCharFormat) -> None:
        cur = self.rich.textCursor()
        if cur.hasSelection():
            cur.mergeCharFormat(fmt)
        self.rich.mergeCurrentCharFormat(fmt)
        self.rich.setFocus()
        self._update_toolbar_state()

    # ------------------------------------------------------------------
    # Block formatting
    # ------------------------------------------------------------------
    def _selected_blocks(self) -> List[QTextBlock]:
        cur = self.rich.textCursor()
        doc = self.rich.document()
        start = doc.findBlock(cur.selectionStart())
        end = doc.findBlock(cur.selectionEnd())
        blocks = [start]
        b = start
        while b.isValid() and b != end:
            b = b.next()
            if b.isValid():
                blocks.append(b)
        return blocks

    def _apply_block_char_format(self, block: QTextBlock, fmt: QTextCharFormat) -> None:
        c = QTextCursor(block)
        c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        c.mergeCharFormat(fmt)
        c.mergeBlockCharFormat(fmt)

    def toggle_heading(self, level: int) -> None:
        if self._mode == self.MARKDOWN:
            return self._toggle_line_prefix("#" * level + " ", r"#{1,6}\s")
        blocks = self._selected_blocks()
        target = 0 if blocks[0].blockFormat().headingLevel() == level else level
        cur = self.rich.textCursor()
        cur.beginEditBlock()
        for block in blocks:
            bf = block.blockFormat()
            bf.setHeadingLevel(target)
            QTextCursor(block).setBlockFormat(bf)
            cf = QTextCharFormat()
            cf.setProperty(QTextFormat.FontSizeAdjustment, _HEADING_SIZE.get(target, 0))
            cf.setFontWeight(QFont.Bold if target else QFont.Normal)
            self._apply_block_char_format(block, cf)
        cur.endEditBlock()
        self._update_toolbar_state()

    def _toggle_list(self, style: QTextListFormat.Style, task: bool) -> None:
        blocks = self._selected_blocks()
        first = blocks[0]
        current = first.textList()
        no_marker = QTextBlockFormat.MarkerType.NoMarker
        already = (
            current is not None
            and current.format().style() == style
            and (first.blockFormat().marker() != no_marker) == task
        )
        cur = self.rich.textCursor()
        cur.beginEditBlock()
        if already:
            for block in blocks:
                lst = block.textList()
                if lst:
                    lst.remove(block)
                bf = block.blockFormat()
                bf.setIndent(0)
                bf.setMarker(no_marker)
                QTextCursor(block).setBlockFormat(bf)
        else:
            lf = QTextListFormat()
            lf.setStyle(style)
            lf.setIndent(1)
            if current is not None and all(b.textList() == current for b in blocks):
                current.setFormat(lf)
            else:
                cur.createList(lf)
            for block in self._selected_blocks():
                bf = block.blockFormat()
                if task:
                    if bf.marker() == no_marker:
                        bf.setMarker(QTextBlockFormat.MarkerType.Unchecked)
                else:
                    bf.setMarker(no_marker)
                QTextCursor(block).setBlockFormat(bf)
        cur.endEditBlock()
        self._update_toolbar_state()

    def toggle_bullets(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._toggle_line_prefix("- ", r"[-*+]\s(?!\[[ xX]\])")
        self._toggle_list(QTextListFormat.ListDisc, task=False)

    def toggle_numbers(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._toggle_line_prefix("1. ", r"\d+[.)]\s", numbered=True)
        self._toggle_list(QTextListFormat.ListDecimal, task=False)

    def toggle_tasks(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._toggle_line_prefix("- [ ] ", r"[-*+]\s\[[ xX]\]\s")
        self._toggle_list(QTextListFormat.ListDisc, task=True)

    def toggle_quote(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._toggle_line_prefix("> ", r">\s?")
        blocks = self._selected_blocks()
        quoted = bool(blocks[0].blockFormat().property(QTextFormat.BlockQuoteLevel))
        cur = self.rich.textCursor()
        cur.beginEditBlock()
        for block in blocks:
            bf = block.blockFormat()
            if quoted:
                bf.clearProperty(QTextFormat.BlockQuoteLevel)
                bf.setLeftMargin(0)
            else:
                bf.setProperty(QTextFormat.BlockQuoteLevel, 1)
                bf.setLeftMargin(40)
            QTextCursor(block).setBlockFormat(bf)
        cur.endEditBlock()
        self._update_toolbar_state()

    def toggle_code_block(self) -> None:
        if self._mode == self.MARKDOWN:
            return self._wrap_source_lines("```\n", "\n```", "code")
        blocks = self._selected_blocks()
        is_code = blocks[0].blockFormat().hasProperty(QTextFormat.BlockCodeFence)
        cur = self.rich.textCursor()
        cur.beginEditBlock()
        for block in blocks:
            bf = block.blockFormat()
            cf = QTextCharFormat()
            if is_code:
                bf.clearProperty(QTextFormat.BlockCodeFence)
                bf.setNonBreakableLines(False)
                cf.setFontFixedPitch(False)
                cf.setFontFamilies(self.rich.document().defaultFont().families())
            else:
                bf.setProperty(QTextFormat.BlockCodeFence, "`")
                bf.setNonBreakableLines(True)
                cf.setFontFixedPitch(True)
                cf.setFontFamilies(_fixed_font_families())
            QTextCursor(block).setBlockFormat(bf)
            self._apply_block_char_format(block, cf)
        cur.endEditBlock()
        self._update_toolbar_state()

    # ------------------------------------------------------------------
    # Markdown-source helpers
    # ------------------------------------------------------------------
    def _wrap_source(self, prefix: str, suffix: str, placeholder: str) -> None:
        cur = self.source.textCursor()
        text = cur.selectedText()
        if text and text.startswith(prefix) and text.endswith(suffix) and len(text) >= len(prefix) + len(suffix):
            cur.insertText(text[len(prefix): len(text) - len(suffix)])
        elif text:
            cur.insertText(f"{prefix}{text}{suffix}")
        else:
            start = cur.position() + len(prefix)
            cur.insertText(f"{prefix}{placeholder}{suffix}")
            cur.setPosition(start)
            cur.setPosition(start + len(placeholder), QTextCursor.KeepAnchor)
        self.source.setTextCursor(cur)
        self.source.setFocus()

    def _wrap_source_lines(self, prefix: str, suffix: str, placeholder: str) -> None:
        cur = self.source.textCursor()
        text = cur.selection().toPlainText() if cur.hasSelection() else ""
        lead = "\n" if cur.selectionStart() > 0 and cur.block().position() != cur.selectionStart() else ""
        cur.insertText(f"{lead}{prefix}{text or placeholder}{suffix}\n")
        self.source.setTextCursor(cur)
        self.source.setFocus()

    def _toggle_line_prefix(self, prefix: str, existing_pattern: str, numbered: bool = False) -> None:
        cur = self.source.textCursor()
        doc = self.source.document()
        start = doc.findBlock(cur.selectionStart())
        end = doc.findBlock(cur.selectionEnd())
        blocks = []
        b = start
        while b.isValid():
            blocks.append(b)
            if b == end:
                break
            b = b.next()
        pat = re.compile(r"^(\s*)" + existing_pattern)
        remove = all(pat.match(b.text()) for b in blocks)
        edit = QTextCursor(doc)
        edit.beginEditBlock()
        for i, block in enumerate(blocks):
            text = block.text()
            if remove:
                new = pat.sub(r"\1", text, count=1)
            else:
                stripped = re.sub(r"^(\s*)(#{1,6}\s|>\s?|[-*+]\s(\[[ xX]\]\s)?|\d+[.)]\s)", r"\1", text, count=1)
                new = (f"{i + 1}. " if numbered else prefix) + stripped
            c = QTextCursor(block)
            c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
            c.insertText(new)
        edit.endEditBlock()
        self.source.setFocus()

    # ------------------------------------------------------------------
    # Toolbar state
    # ------------------------------------------------------------------
    def _update_toolbar_state(self) -> None:
        if self._mode != self.VISUAL:
            return
        cur = self.rich.textCursor()
        cf = self.rich.currentCharFormat()
        bf = cur.blockFormat()
        lst = cur.currentList()
        marker = bf.marker() != QTextBlockFormat.MarkerType.NoMarker
        states = {
            "bold": cf.fontWeight() > QFont.Normal and bf.headingLevel() == 0,
            "italic": cf.fontItalic(),
            "strike": cf.fontStrikeOut(),
            "code": _is_code_format(cf) and not bf.hasProperty(QTextFormat.BlockCodeFence),
            "h1": bf.headingLevel() == 1,
            "h2": bf.headingLevel() == 2,
            "h3": bf.headingLevel() == 3,
            "bullets": lst is not None and lst.format().style() == QTextListFormat.ListDisc and not marker,
            "numbers": lst is not None and lst.format().style() == QTextListFormat.ListDecimal,
            "tasks": lst is not None and marker,
            "quote": bool(bf.property(QTextFormat.BlockQuoteLevel)),
            "codeblock": bf.hasProperty(QTextFormat.BlockCodeFence),
        }
        for key, on in states.items():
            btn = self._buttons[key]
            btn.blockSignals(True)
            btn.setChecked(bool(on))
            btn.blockSignals(False)

    def sizeHint(self) -> QSize:
        return QSize(520, 260)
