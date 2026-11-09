"""
Markdown Helpers (pure Python, no Qt)

Task descriptions and comments are stored as Markdown. Inline images and links
to task attachments use the ``attachment:<id>`` URL scheme, e.g.
``![wireframe](attachment:12)``. Attachments staged in the editor but not yet
saved use a temporary ``attachment:new-<n>`` reference that is rewritten to the
real id on save.
"""
from __future__ import annotations

import re
from typing import Dict, Set

ATTACHMENT_SCHEME = "attachment"

_REF = r"[A-Za-z0-9_-]+"
_ANY_REF = re.compile(rf"\]\({ATTACHMENT_SCHEME}:({_REF})(\s+\"[^\"]*\")?\)")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def attachment_url(ref: object) -> str:
    return f"{ATTACHMENT_SCHEME}:{ref}"


def parse_attachment_url(url: str) -> str | None:
    """Return the reference part of an ``attachment:<ref>`` URL, or None."""
    prefix = ATTACHMENT_SCHEME + ":"
    if url.startswith(prefix):
        ref = url[len(prefix):].strip("/")
        return ref if re.fullmatch(_REF, ref) else None
    return None


def attachment_refs(markdown: str) -> Set[str]:
    """All attachment references (image or link) used in the text."""
    return {m.group(1) for m in _ANY_REF.finditer(markdown or "")}


def rewrite_attachment_refs(markdown: str, mapping: Dict[str, str]) -> str:
    """Replace ``attachment:<old>`` references according to ``mapping`` (old ref -> new ref)."""
    if not markdown or not mapping:
        return markdown

    def sub(m: re.Match) -> str:
        ref = m.group(1)
        return f"]({attachment_url(mapping.get(ref, ref))}{m.group(2) or ''})"

    return _ANY_REF.sub(sub, markdown)


def remove_attachment_refs(markdown: str, ref: str) -> str:
    """Drop inline images of an attachment and unlink (keep the text of) links to it."""
    if not markdown:
        return markdown
    url = re.escape(attachment_url(ref))
    text = re.sub(rf"!\[[^\]]*\]\({url}(?:\s+\"[^\"]*\")?\)[ \t]*\n?", "", markdown)
    text = re.sub(rf"\[([^\]]*)\]\({url}(?:\s+\"[^\"]*\")?\)", r"\1", text)
    return text


def markdown_to_plain(markdown: str) -> str:
    """Best-effort conversion of Markdown to readable plain text (for previews/snippets)."""
    if not markdown:
        return ""
    out = []
    in_fence = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(line.strip())
            continue
        if re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", line):
            continue  # table separator row
        if re.match(r"^\s*([-*_]\s*){3,}$", line):
            continue  # horizontal rule
        line = _IMAGE.sub(lambda m: "", line)
        line = _LINK.sub(lambda m: m.group(1), line)
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)          # headings
        line = re.sub(r"^\s*(>\s*)+", "", line)                 # blockquotes
        line = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line)      # list markers
        line = re.sub(r"^\[( |x|X)\]\s+", "", line)             # task-list boxes
        line = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", line)      # strong / strike
        line = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", line)  # emphasis
        line = re.sub(r"`+([^`]*)`+", r"\1", line)              # inline code
        if line.strip().startswith("|"):
            line = " ".join(c.strip() for c in line.strip().strip("|").split("|"))
        line = line.replace("\\", "")
        out.append(line.strip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def first_line_snippet(markdown: str, limit: int = 80) -> str:
    """A one-line preview for task cards: the first line of body text, truncated.

    Headings ("## Summary") say little on their own, so body text is preferred
    and the first heading is only used when there is nothing else.
    """
    def clip(text: str) -> str:
        return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

    fallback = ""
    in_fence = False
    for raw in (markdown or "").splitlines():
        if re.match(r"^\s*(```|~~~)", raw):
            in_fence = not in_fence
            continue
        is_heading = not in_fence and re.match(r"^\s{0,3}#{1,6}\s", raw)
        text = markdown_to_plain(raw).strip()
        if not text:
            continue
        if is_heading:
            fallback = fallback or text
            continue
        return clip(text)
    return clip(fallback)


def tidy_qt_markdown(markdown: str) -> str:
    """Clean up quirks of QTextDocument.toMarkdown() output.

    Qt emits a stray blank line before a closing code fence (and sometimes
    indents the fence), and omits the blank line after it. Trailing whitespace
    is also stripped so stored descriptions stay tidy.
    """
    lines = (markdown or "").split("\n")
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        stripped = line.strip()
        if fence is None:
            m = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if m:
                fence = m.group(1)
            out.append(line)
            continue
        if stripped.startswith(fence) and stripped.strip(fence[0]) == "":
            while out and out[-1].strip() == "":
                out.pop()
            out.append(fence)
            fence = None
            out.append("\x00")  # marker: a closing fence was just emitted
            continue
        out.append(line)
    result: list[str] = []
    for i, line in enumerate(out):
        if line == "\x00":
            nxt = out[i + 1] if i + 1 < len(out) else ""
            if nxt not in ("", "\x00"):
                result.append("")
            continue
        result.append(line.rstrip())
    return "\n".join(result).strip("\n")
