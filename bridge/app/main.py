"""OpenWA bridge: a POST-in / WhatsApp-out endpoint that logs everything to MongoDB.

    POST /send            {"id": "919876543210", "msg": "hello"}   -> sends via OpenWA
    POST /webhooks/openwa                                          -> the gateway delivers events here

Both directions land in one MongoDB collection.
"""

import asyncio
import base64
import binascii
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import media
from . import messages as msg_map
from .config import get_settings
from .db import Store, utcnow
from .models import HealthResponse, SendRequest, SendResponse
from .numbers import InvalidNumber, phone_of, to_chat_id
from .openwa import OpenWAClient, OpenWAError
from .security import require_api_key, verify_openwa_signature

MAX_TEXT = 4096  # WhatsApp's per-message limit, enforced by OpenWA

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("bridge")

store = Store(
    settings.mongo_uri,
    settings.mongo_db,
    settings.mongo_collection,
    settings.mongo_events_collection,
)
openwa = OpenWAClient(
    settings.openwa_base_url,
    settings.openwa_api_key,
    settings.openwa_session_name,
    settings.openwa_timeout,
)

# Media downloads run detached from the delivery that triggered them, so the
# gateway is not kept waiting on a video. asyncio only holds weak references to
# tasks, and one that gets collected mid-flight simply vanishes.
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.startup_problems()
    if problems:
        for problem in problems:
            log.error("config: %s", problem)
        raise SystemExit("Refusing to start with an unsafe configuration - fix bridge/.env")

    await store.connect()

    # Both steps are best-effort: the bridge still starts when OpenWA is not up
    # yet, and POST /events/register retries them on demand.
    if settings.events_auto_register:
        try:
            found = await openwa.ensure_session()
            log.info(
                "session '%s' %s (status: %s)",
                settings.openwa_session_name,
                found["action"],
                found["session"].get("status"),
            )
            result = await openwa.ensure_webhook(
                settings.events_url, settings.events_list, settings.events_secret
            )
            log.info("event subscription %s -> %s", result["action"], settings.events_url)
        except OpenWAError as exc:
            log.warning("could not register the event subscription yet (%s)", exc)

    log.info(
        "bridge ready on %s:%s  session=%s  mongo=%s/%s",
        settings.bridge_host,
        settings.bridge_port,
        settings.openwa_session_name,
        settings.mongo_db,
        settings.mongo_collection,
    )
    try:
        yield
    finally:
        # Let media downloads in flight finish writing rather than leaving a
        # half-written file on disk with a path already recorded in MongoDB.
        if _background:
            log.info("waiting for %d media download(s) to finish", len(_background))
            await asyncio.wait(set(_background), timeout=30)
        await openwa.close()
        await store.close()


app = FastAPI(
    title="OpenWA Bridge",
    description="Send WhatsApp messages over HTTP and archive every message in MongoDB.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(OpenWAError)
async def _openwa_error_handler(request: Request, exc: OpenWAError) -> JSONResponse:
    # A 4xx from OpenWA is usually the caller's input; anything else is ours.
    # 401 is the exception: that is the bridge's own gateway key being wrong.
    status = 502
    if exc.status and 400 <= exc.status < 500 and exc.status != 401:
        status = exc.status
    return JSONResponse(status_code=status, content={"ok": False, "error": str(exc)})


# ---------------------------------------------------------------- public ---


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "openwa-bridge",
        "send": {
            "method": "POST",
            "path": "/send",
            "headers": {
                "Authorization": "Bearer <BRIDGE_API_KEY>",
                "Content-Type": "application/json",
            },
            "body": {"id": "919876543210", "msg": "your message"},
        },
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    mongo_ok = await store.ping()
    openwa_ok = False
    session: dict[str, Any] | None = None
    detail = None
    try:
        info = await openwa.session_info()
        openwa_ok = True
        session = {
            "name": info.get("name"),
            "status": info.get("status"),
            "phone": info.get("phone"),
        }
    except OpenWAError as exc:
        detail = str(exc)

    return HealthResponse(
        ok=mongo_ok and openwa_ok and (session or {}).get("status") == "ready",
        mongo=mongo_ok,
        openwa=openwa_ok,
        session=session,
        detail=detail,
    )


# ------------------------------------------------------------------ send ---


@app.post("/send", response_model=SendResponse, dependencies=[Depends(require_api_key)])
async def send(payload: SendRequest) -> SendResponse:
    try:
        chat_id = to_chat_id(payload.id, settings.default_country_code)
    except InvalidNumber as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sent_at = utcnow()
    try:
        result = await openwa.send_text(chat_id, payload.msg)
    except OpenWAError as exc:
        # Record the attempt so a failed send is not invisible in Mongo.
        await store.upsert_message(
            session_name=settings.openwa_session_name,
            message_id=None,
            set_fields={
                "direction": "out",
                "chatId": chat_id,
                "phone": phone_of(chat_id),
                "body": payload.msg,
                "type": "text",
                "fromMe": True,
                "status": "failed",
                "error": str(exc),
                "timestamp": sent_at,
                "source": "api",
            },
        )
        raise

    message_id = (result or {}).get("messageId")
    doc = await store.upsert_message(
        session_name=settings.openwa_session_name,
        message_id=message_id,
        set_fields={
            "direction": "out",
            "chatId": chat_id,
            "phone": phone_of(chat_id),
            "body": payload.msg,
            "type": "text",
            "fromMe": True,
            "source": "api",
        },
        # The message.sent webhook can land before this write returns, so status
        # and timestamp are insert-only: never clobber what the engine reported.
        on_insert={"status": "sent", "timestamp": sent_at},
    )

    log.info("sent -> %s (%s)", chat_id, message_id)
    return SendResponse(
        ok=True,
        messageId=message_id,
        chatId=chat_id,
        status=doc.get("status", "sent"),
        storedId=str(doc.get("_id")) if doc else None,
    )


# ----------------------------------------------------------------- media ---


async def _save_media(
    message_id: str,
    chat_id: str,
    phone: str | None,
    inline: str | None = None,
    info: dict[str, Any] | None = None,
) -> None:
    """Write one message's media to disk and record where it went.

    The gateway inlines the file as base64 on the webhook itself, so the usual
    path decodes what already arrived - no second request, and nothing to fail.
    The download endpoint is the fallback for when it withholds the bytes
    (`omitted`), which it does for anything over its own size cap.

    Detached from the delivery that triggered it, so the gateway gets its 200
    immediately. Every failure is recorded on the message: a photo that could
    not be saved should say so on the row, not only in a log line nobody reads.
    """
    session_name = settings.openwa_session_name
    info = info or {}

    if inline:
        try:
            content = base64.b64decode(inline, validate=True)
        except (ValueError, binascii.Error) as exc:
            log.warning("media: %s has undecodable inline data (%s)", message_id, exc)
            await store.record_media(
                session_name=session_name,
                message_id=message_id,
                media={"error": f"inline data is not valid base64: {exc}", "savedAt": utcnow()},
            )
            return
        mimetype = info.get("mimetype") or "application/octet-stream"
        filename = info.get("filename")
        await _write_media(message_id, phone, content, mimetype, filename)
        return

    try:
        content, mimetype, filename = await openwa.download_media(chat_id, message_id)
    except OpenWAError as exc:
        # 404 is routine - no stored media, over the gateway's size cap, or a
        # URL-based send, which stores nothing.
        level = log.info if exc.status == 404 else log.warning
        level("media: nothing to fetch for %s (%s)", message_id, exc)
        await store.record_media(
            session_name=session_name,
            message_id=message_id,
            media={"error": str(exc), "savedAt": utcnow()},
        )
        return

    await _write_media(message_id, phone, content, mimetype, filename)


async def _write_media(
    message_id: str,
    phone: str | None,
    content: bytes,
    mimetype: str,
    filename: str | None,
) -> None:
    """Write the bytes to disk and record the result on the message."""
    session_name = settings.openwa_session_name

    if len(content) > settings.media_max_bytes:
        log.warning(
            "media: %s is %d bytes, over the %d limit - not saved",
            message_id,
            len(content),
            settings.media_max_bytes,
        )
        await store.record_media(
            session_name=session_name,
            message_id=message_id,
            media={
                "error": f"{len(content)} bytes exceeds MEDIA_MAX_BYTES ({settings.media_max_bytes})",
                "sizeBytes": len(content),
                "mimetype": mimetype,
                "savedAt": utcnow(),
            },
        )
        return

    root = settings.media_root
    saved = await asyncio.to_thread(
        media.save,
        root,
        content,
        phone=phone,
        message_id=message_id,
        mimetype=mimetype,
        filename=filename,
    )
    await store.record_media(
        session_name=session_name, message_id=message_id, media=saved.as_document(root)
    )
    log.info("media saved: %s (%s, %d bytes)", saved.path, saved.mimetype, saved.size_bytes)


async def _enrich(
    message_id: str,
    chat_id: str,
    phone: str | None,
    *,
    inline: str | None,
    info: dict[str, Any] | None,
    save_media: bool,
) -> None:
    """Fill in what the webhook could not tell us, then store the media.

    Resolution comes first so the media filename can carry the number too.
    Both run in one detached task, so the gateway still gets its 200 straight
    away rather than waiting on a lookup and a file write.
    """
    try:
        # An `@lid` is a privacy id, not a number. Inbound messages get
        # `senderPhone` attached by the gateway, but an outbound one does not -
        # its sender is you - so the recipient has to be resolved on demand.
        if not phone and chat_id and chat_id.endswith("@lid"):
            resolved = await openwa.contact_phone(chat_id)
            if resolved:
                phone = resolved
                await store.set_fields(
                    session_name=settings.openwa_session_name,
                    message_id=message_id,
                    fields={"contactNumber": resolved, "contactIdResolved": True},
                )
                log.info("resolved %s -> %s", chat_id, resolved)
            else:
                log.debug("could not resolve %s to a number", chat_id)

        if save_media:
            await _save_media(message_id, chat_id, phone, inline=inline, info=info)
    except Exception:
        log.exception("enrich: unhandled failure for %s", message_id)


def _spawn(coro) -> None:
    """Run detached, keeping a strong reference so it is not collected."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# --------------------------------------------------------------- webhook ---


@app.post(settings.events_path, include_in_schema=False)
async def openwa_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    await verify_openwa_signature(request, raw_body)

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body is not valid JSON.")

    event = payload.get("event")
    data = payload.get("data") or {}
    session_name = settings.openwa_session_name

    if settings.store_raw_events:
        await store.record_event(payload, dict(request.headers))

    if event in (msg_map.INBOUND_EVENT, msg_map.OUTBOUND_EVENT):
        fields = msg_map.message_fields(event, data)
        message_id = fields.pop("messageId", None)
        await store.upsert_message(
            session_name=session_name,
            message_id=message_id,
            set_fields=fields,
            on_insert={"status": "received" if event == msg_map.INBOUND_EVENT else "sent"},
        )
        log.info("%s %s %s", event, fields.get("chatId"), (fields.get("body") or "")[:60])

        # Two things the webhook cannot give us: an @lid's real number, and the
        # media file once its inline copy has been stripped. Both are handled
        # in one detached task, claimed atomically so a redelivered event does
        # neither twice.
        chat_id = fields.get("chatId")
        wanted = event == msg_map.INBOUND_EVENT or settings.media_outbound
        save_media = bool(settings.media_enabled and wanted and fields.get("hasMedia"))
        needs_number = bool(
            not fields.get("contactNumber") and chat_id and chat_id.endswith("@lid")
        )

        if (
            message_id
            and chat_id
            and (save_media or needs_number)
            and await store.claim_media(session_name=session_name, message_id=message_id)
        ):
            _spawn(
                _enrich(
                    message_id,
                    chat_id,
                    fields.get("contactNumber"),
                    inline=msg_map.inline_media(data),
                    info=fields.get("mediaInfo"),
                    save_media=save_media,
                )
            )

    elif event in ("message.ack", "message.failed"):
        message_id, status = msg_map.ack_fields(data)
        if message_id and status:
            matched = await store.update_status(
                session_name=session_name, message_id=message_id, status=status
            )
            if not matched:
                log.debug("ack for unknown message %s (%s)", message_id, status)

    elif event == "message.revoked":
        target = msg_map.revoked_target(data)
        if target:
            await store.update_status(
                session_name=session_name,
                message_id=target,
                status="revoked",
                extra={"revokedAt": utcnow()},
            )

    elif event == "message.edited":
        message_id = data.get("messageId")
        if message_id:
            await store.update_status(
                session_name=session_name,
                message_id=message_id,
                status="edited",
                extra={"body": data.get("body"), "editedAt": utcnow()},
            )

    elif event == "session.status":
        log.info("session %s -> %s", data.get("sessionId"), data.get("status"))

    return {"ok": True}


# ------------------------------------------------------------- read-only ---


@app.get("/messages", dependencies=[Depends(require_api_key)])
async def list_messages(
    limit: int = Query(50, ge=1, le=500),
    chatId: str | None = None,
    direction: str | None = Query(None, pattern="^(in|out)$"),
) -> dict[str, Any]:
    rows = await store.recent(limit=limit, chat_id=chatId, direction=direction)
    return {"count": len(rows), "messages": rows}


@app.get("/session", dependencies=[Depends(require_api_key)])
async def session_info() -> dict[str, Any]:
    return await openwa.session_info()


@app.get("/qr", dependencies=[Depends(require_api_key)])
async def qr() -> dict[str, Any]:
    """The pairing QR as a PNG data URL (also shown in the OpenWA dashboard)."""
    return await openwa.qr()


@app.post("/events/register", dependencies=[Depends(require_api_key)])
async def register_events() -> dict[str, Any]:
    """Re-create the session and event subscription OpenWA delivers to.

    Only needed when the bridge started before OpenWA did, or the subscription
    was deleted in the dashboard.
    """
    found = await openwa.ensure_session()
    result = await openwa.ensure_webhook(
        settings.events_url, settings.events_list, settings.events_secret
    )
    return {
        "ok": True,
        "session": found["action"],
        "subscription": result["action"],
        "url": settings.events_url,
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.bridge_host,
        port=settings.bridge_port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    sys.exit(main())
