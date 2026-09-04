"""Saving a message's media to disk and recording where it went.

WhatsApp media does not arrive in the webhook. The event only says
`hasMedia: true`; the bytes are fetched separately from

    GET /api/sessions/{session}/messages/{chatId}/{messageId}/media

which serves the gateway's archived copy, falling back to the inline copy it
keeps on the message row. So this module runs *after* a message is stored:
fetch, write to disk, then record the path on the document.

Files are laid out by date, which keeps any one directory small enough to open:

    data/media/2026/09/04/919876543210_3EB0ABCD.jpg

The name is built from the counterparty and a slice of the message id, both
sanitised - a raw WhatsApp id (`true_919...@c.us_3EB0`) contains characters
Windows will not accept in a filename.
"""

import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("bridge.media")

UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Extensions mimetypes.guess_extension gets wrong or misses for what WhatsApp
# actually sends. Left to the stdlib, a JPEG becomes .jpe and an ogg voice note
# resolves to nothing at all.
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "text/plain": ".txt",
}


@dataclass(frozen=True)
class SavedMedia:
    path: str
    filename: str
    mimetype: str
    size_bytes: int

    def as_document(self, root: Path) -> dict[str, Any]:
        """What gets stored on the message row.

        Both paths are kept: `path` is relative, so the archive survives the
        project being moved or read from another machine, and `absolutePath` is
        the convenience for opening it right now on this one.
        """
        return {
            "path": self.path,
            "absolutePath": str((root / self.path).resolve()),
            "filename": self.filename,
            "mimetype": self.mimetype,
            "sizeBytes": self.size_bytes,
            "savedAt": datetime.now(timezone.utc),
        }


def extension_for(mimetype: str, filename: str | None) -> str:
    """Pick a file extension, preferring the sender's own filename."""
    if filename:
        suffix = Path(filename).suffix
        if suffix and len(suffix) <= 10:
            return suffix.lower()
    base = (mimetype or "").split(";")[0].strip().lower()
    if base in EXTENSIONS:
        return EXTENSIONS[base]
    guessed = mimetypes.guess_extension(base) if base else None
    return guessed or ".bin"


def safe_stem(phone: str | None, message_id: str) -> str:
    """A filename stem that is unique per message and legal on Windows.

    Uniqueness comes from a digest of the WHOLE id, never from a slice of it.
    An outbound id looks like `true_2590...@lid_3EB051DEB650C17ECF2511_out`, so
    taking the last underscore-separated segment yields the literal string
    "out" for every outbound message - and each media file would overwrite the
    last. The readable middle is kept only to make the directory browsable.
    """
    who = UNSAFE.sub("", phone or "") or "chat"
    readable = UNSAFE.sub("", message_id)[-18:] or "msg"
    digest = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:10]
    return f"{who}_{readable}_{digest}"


def target_path(
    root: Path,
    *,
    phone: str | None,
    message_id: str,
    mimetype: str,
    filename: str | None,
    when: datetime | None = None,
) -> Path:
    moment = when or datetime.now(timezone.utc)
    folder = root / f"{moment:%Y}" / f"{moment:%m}" / f"{moment:%d}"
    return folder / f"{safe_stem(phone, message_id)}{extension_for(mimetype, filename)}"


def save(
    root: Path,
    content: bytes,
    *,
    phone: str | None,
    message_id: str,
    mimetype: str,
    filename: str | None,
    when: datetime | None = None,
) -> SavedMedia:
    """Write the bytes and describe where they landed."""
    destination = target_path(
        root,
        phone=phone,
        message_id=message_id,
        mimetype=mimetype,
        filename=filename,
        when=when,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)

    return SavedMedia(
        # Stored with forward slashes so the value reads the same whatever
        # platform later queries MongoDB.
        path=destination.relative_to(root).as_posix(),
        filename=filename or destination.name,
        mimetype=(mimetype or "application/octet-stream").split(";")[0].strip(),
        size_bytes=len(content),
    )
