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

import base64
import binascii
import hashlib
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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


# Which gateway endpoint sends a given file. WhatsApp treats these as different
# message types, not as one "attachment" with a MIME label: an image gets a
# preview bubble, a document gets a filename and a download icon. Sending a
# photo as a document technically works and looks wrong to the person reading
# it, so the mimetype picks the endpoint and anything unrecognised becomes a
# document - the one type that renders acceptably for arbitrary bytes.
@dataclass
class MediaRef:
    """Where the bytes for an outbound send come from."""

    url: str | None = None
    data_base64: str | None = None
    mimetype: str | None = None
    filename: str | None = None


DATA_URI = re.compile(r"^data:([^;,]*)(;[^,]*)?,", re.I)


def parse_media_ref(value: str) -> MediaRef:
    """Work out what the caller put in `media`.

    Three shapes are accepted because all three turn up in practice: a URL for
    something already hosted, a `data:` URI for what a browser hands you, and
    bare base64 for everything else. Any other scheme is refused rather than
    passed along - `file:///etc/passwd` reaching the gateway would ask it to
    read its own disk and send the result to a stranger.
    """
    raw = (value or "").strip()
    if not raw:
        raise ValueError("media is empty")

    lowered = raw.lower()
    if lowered.startswith(("http://", "https://")):
        name = unquote(urlparse(raw).path.rsplit("/", 1)[-1]) or None
        guessed, _ = mimetypes.guess_type(name) if name else (None, None)
        return MediaRef(url=raw, filename=name, mimetype=guessed)

    match = DATA_URI.match(raw)
    if match:
        if ";base64" not in (match.group(2) or "").lower():
            raise ValueError("a data: URI must be base64-encoded")
        return MediaRef(
            data_base64=raw[match.end():].strip(),
            mimetype=(match.group(1) or "").strip() or None,
        )

    if "://" in raw[:24]:
        scheme = raw.split("://", 1)[0]
        raise ValueError(f"media must be an http(s) URL, a data: URI or base64 - not {scheme}://")

    # Bare base64. Validate now: the gateway would otherwise answer with its own
    # decoder's error, which says nothing about which field was wrong.
    try:
        base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"media is not a URL, a data: URI, or valid base64 ({exc})") from exc
    return MediaRef(data_base64=raw)


# Leading bytes that identify a format. A filename and a caller-supplied
# mimetype are both claims; these are evidence. Ordered longest-first where
# prefixes overlap so the more specific signature wins.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
    (b"PK\x03\x04", "application/zip"),
    (b"BM", "image/bmp"),
)

# Signatures that need a second marker further into the file.
CONTAINERS: tuple[tuple[bytes, int, bytes, str], ...] = (
    (b"RIFF", 8, b"WEBP", "image/webp"),
    (b"RIFF", 8, b"WAVE", "audio/wav"),
    (b"RIFF", 8, b"AVI ", "video/x-msvideo"),
)

HTML_MARKERS = (b"<!doctype html", b"<html", b"<?xml", b"<!--")


def sniff(content: bytes) -> str | None:
    """The mimetype the bytes actually are, or None when nothing is recognised.

    This exists because of how badly the alternative fails. `curl -o photo.jpg`
    against a URL that answers 400 writes the error page to photo.jpg, and every
    later step believes the extension: the bridge sends it as an image, the
    engine hands WhatsApp an HTML document, and the reply is a 500 that mentions
    none of it. Bytes are the only part of that chain that cannot lie.
    """
    if not content:
        return None

    head = content[:32]
    for magic, mimetype in SIGNATURES:
        if head.startswith(magic):
            return mimetype
    for magic, offset, marker, mimetype in CONTAINERS:
        if head.startswith(magic) and content[offset : offset + len(marker)] == marker:
            return mimetype
    # ISO base media (mp4, m4a, mov): a size field, then 'ftyp'.
    if content[4:8] == b"ftyp":
        brand = content[8:12]
        return "audio/mp4" if brand.startswith(b"M4A") else "video/mp4"
    # MPEG audio frame sync, for an mp3 with no ID3 tag.
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio/mpeg"

    stripped = content[:512].lstrip().lower()
    if any(stripped.startswith(marker) for marker in HTML_MARKERS):
        return "text/html"
    return None


def kind_for(mimetype: str | None, filename: str | None = None) -> str:
    base = (mimetype or "").split(";")[0].strip().lower()
    if not base and filename:
        guessed, _ = mimetypes.guess_type(filename)
        base = (guessed or "").lower()
    if base.startswith("image/"):
        return "image"
    if base.startswith("video/"):
        return "video"
    if base.startswith("audio/"):
        return "audio"
    return "document"


def guess_mimetype(filename: str | None, fallback: str = "application/octet-stream") -> str:
    """Best-effort mimetype from a filename, for uploads that arrive without one."""
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
        suffix = Path(filename).suffix.lower()
        for mime, ext in EXTENSIONS.items():
            if ext == suffix:
                return mime
    return fallback


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
