"""
Content-Addressed File Storage for Task Attachments

Files are stored once per unique content under ``<root>/<sha[:2]>/<sha><ext>``.
Blobs are immutable, so identical files attached to several tasks share one copy,
and backups can mirror blobs incrementally without ever overwriting anything.
The original filename, size and MIME type live in the ``attachments`` table.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Optional

from kanbanmanager.config import MAX_ATTACHMENT_SIZE_MB

_CHUNK = 1024 * 1024
_SAFE_EXT = re.compile(r"^\.[a-z0-9]{1,10}$")


class AttachmentError(ValueError):
    """Raised when a file cannot be attached (missing, too large, unreadable)."""


@dataclass(frozen=True)
class StoredBlob:
    stored_name: str
    checksum: str
    size_bytes: int


def guess_content_type(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or "application/octet-stream"


def format_file_size(size_bytes: int) -> str:
    size = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class AttachmentStore:
    """Stores attachment files on disk, keyed by SHA-256 content hash."""

    def __init__(self, root: Path | str, max_bytes: Optional[int] = None):
        self.root = Path(root)
        self.max_bytes = max_bytes if max_bytes is not None else MAX_ATTACHMENT_SIZE_MB * 1024 * 1024

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def store_file(self, source: Path | str, filename: Optional[str] = None) -> StoredBlob:
        src = Path(source)
        if not src.is_file():
            raise AttachmentError(f"File not found: {src}")
        size = src.stat().st_size
        self._check_size(size, filename or src.name)
        with open(src, "rb") as fh:
            return self._store_stream(fh, filename or src.name)

    def store_bytes(self, data: bytes, filename: str) -> StoredBlob:
        self._check_size(len(data), filename)
        import io

        return self._store_stream(io.BytesIO(data), filename)

    def _check_size(self, size: int, filename: str) -> None:
        if self.max_bytes and size > self.max_bytes:
            raise AttachmentError(
                f"'{filename}' is {format_file_size(size)}; the attachment limit is "
                f"{format_file_size(self.max_bytes)}."
            )

    def _store_stream(self, stream, filename: str) -> StoredBlob:
        self.root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(prefix=".incoming_", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if self.max_bytes and size > self.max_bytes:
                        raise AttachmentError(
                            f"'{filename}' exceeds the attachment limit of {format_file_size(self.max_bytes)}."
                        )
                    digest.update(chunk)
                    out.write(chunk)
            checksum = digest.hexdigest()
            stored_name = f"{checksum[:2]}/{checksum}{self._safe_extension(filename)}"
            dest = self.root / stored_name
            if dest.exists():
                os.unlink(tmp_name)  # identical content already stored
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_name, dest)
            return StoredBlob(stored_name=stored_name, checksum=checksum, size_bytes=size)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _safe_extension(filename: str) -> str:
        ext = PurePath(filename or "").suffix.lower()
        return ext if _SAFE_EXT.match(ext) else ""

    # ------------------------------------------------------------------
    # Reading / housekeeping
    # ------------------------------------------------------------------
    def path_for(self, stored_name: str) -> Path:
        """Absolute path for a stored blob; refuses names that escape the store root."""
        root = self.root.resolve()
        candidate = (root / stored_name).resolve()
        if root != candidate and root not in candidate.parents:
            raise AttachmentError(f"Invalid attachment path: {stored_name}")
        return candidate

    def exists(self, stored_name: str) -> bool:
        try:
            return self.path_for(stored_name).is_file()
        except AttachmentError:
            return False

    def remove(self, stored_name: str) -> None:
        try:
            path = self.path_for(stored_name)
        except AttachmentError:
            return
        if path.is_file():
            path.unlink()
            try:
                path.parent.rmdir()  # drop the shard dir if now empty
            except OSError:
                pass

    def copy_blob_to(self, stored_name: str, dest_root: Path | str) -> bool:
        """Copy one blob into another store root (used by backups). Returns True if copied."""
        src = self.path_for(stored_name)
        if not src.is_file():
            return False
        dest = Path(dest_root) / stored_name
        if dest.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
