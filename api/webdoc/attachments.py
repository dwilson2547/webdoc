"""Attachments: files uploaded to a document (images pasted into the editor,
PDFs, logs). The body links to one as /api/attachments/<id>/<name>, an
ordinary markdown link or image. Only sniffed raster images are served
inline; everything else downloads, so an upload can never run as a page here.

Sniffing and name cleaning are copied from nodewright's attachments.py.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

from .models import Attachment

LINK_RE = re.compile(r"/api/attachments/([0-9a-f]{32})(?:/[^\s)\"'<>]*)?")

# Raster images only: SVG can carry script, so it downloads like any other file.
_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]
INLINE = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}


def sniff(data: bytes) -> str | None:
    """The image type the bytes really are, or None."""
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def content_type(data: bytes, declared: str | None) -> str:
    """Stored type: the sniffed image type, else what the client declared
    (informational only; it is never served as-is)."""
    image = sniff(data)
    if image:
        return image
    declared = (declared or "").split(";")[0].strip().lower()
    if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", declared) and len(declared) <= 100:
        return declared
    return "application/octet-stream"


def clean_name(raw: str | None) -> str:
    name = unicodedata.normalize("NFC", raw or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*')
    name = name.strip(" .")[:200]
    return name or "file"


def url(a: Attachment) -> str:
    return f"/api/attachments/{a.id}/{quote(a.name)}"


def markdown_link(a: Attachment) -> str:
    """What the editor inserts: an image for images, a link for the rest."""
    label = a.name.replace("[", "(").replace("]", ")")
    return f"![{label}]({url(a)})" if a.content_type in INLINE else f"[{label}]({url(a)})"


def out(a: Attachment) -> dict:
    return {"id": a.id, "document_id": a.document_id, "name": a.name,
            "content_type": a.content_type, "size": a.size, "image": a.content_type in INLINE,
            "url": url(a), "markdown": markdown_link(a),
            "by": a.uploader.username if a.uploader else None,
            "created_at": a.created_at.isoformat()}
