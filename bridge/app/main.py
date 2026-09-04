"""OpenWA bridge: a POST-in / WhatsApp-out endpoint that logs everything to MongoDB.

    POST /send            {"id": "919876543210", "msg": "hello"}   -> sends via OpenWA
    POST /webhooks/openwa                                          -> the gateway delivers events here

Both directions land in one MongoDB collection.
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import media
from . import messages as msg_map
from . import poller
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
poll_client = poller.PollClient(settings.poll_bearer, settings.poll_timeout)

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

    poll_task: asyncio.Task | None = None
    if settings.poll_enabled:
        poll_task = asyncio.create_task(_poll_loop())
    else:
        log.info("polling is off (POLL_URL is empty)")

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
        if poll_task is not None:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        # Let media downloads in flight finish writing rather than leaving a
        # half-written file on disk with a path already recorded in MongoDB.
        if _background:
            log.info("waiting for %d media download(s) to finish", len(_background))
            await asyncio.wait(set(_background), timeout=30)
        await poll_client.close()
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


# --------------------------------------------------------------- sending ---


async def _send_and_store(
    chat_id: str,
    text: str,
    *,
    source: str | None = None,
    poll_message_id: str | None = None,
) -> str | None:
    """Send text through OpenWA and record it, the same way POST /send does."""
    sent_at = utcnow()
    result = await openwa.send_text(chat_id, text)
    message_id = (result or {}).get("messageId")
    await store.upsert_message(
        session_name=settings.openwa_session_name,
        message_id=message_id,
        set_fields={
            "direction": "out",
            "chatId": chat_id,
            "phone": phone_of(chat_id),
            "body": text,
            "type": "text",
            "fromMe": True,
            "source": source or "api",
            "pollMessageId": poll_message_id or None,
        },
        on_insert={"status": "sent", "timestamp": sent_at},
    )
    return message_id


# ----------------------------------------------------------------- media ---


async def _save_media(message_id: str, chat_id: str, phone: str | None) -> None:
    """Fetch one message's media and record where it was written.

    Detached from the delivery that triggered it, so the gateway gets its 200
    immediately rather than waiting on a video download. Every failure is
    recorded on the message: a photo that could not be fetched should say so on
    the row, not only in a log line nobody reads.
    """
    session_name = settings.openwa_session_name
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


def _spawn(coro) -> None:
    """Run detached, keeping a strong reference so it is not collected."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ------------------------------------------------------------------ poll ---


class _PollState:
    """What the loop has to remember between polls.

    `last_text` backs dedup rule 2 and `endpoint_dequeues` retires it - see
    poller.py for why suppressing a message from a dequeuing endpoint destroys
    it rather than deferring it.
    """

    def __init__(self) -> None:
        self.last_text: dict[str, str] = {}
        self.endpoint_dequeues = False
        self.session_ready = False


poll_state = _PollState()


async def _session_is_ready() -> bool:
    try:
        info = await openwa.session_info()
    except OpenWAError as exc:
        log.debug("poll: cannot read session status (%s)", exc)
        return False
    return info.get("status") == "ready"


async def _deliver_polled(item: poller.Outgoing) -> None:
    """Send one polled message, recording it whatever happens.

    A dequeuing endpoint has already given the message up by the time we see it,
    so a failure here loses it unless it is written down. It is written down.
    """
    try:
        chat_id = to_chat_id(item.dest, settings.default_country_code)
    except InvalidNumber as exc:
        log.error("poll: cannot address %r (%s) - message dropped: %r", item.dest, exc, item.text[:60])
        await store.upsert_message(
            session_name=settings.openwa_session_name,
            message_id=None,
            set_fields={
                "direction": "out",
                "body": item.text,
                "type": "text",
                "fromMe": True,
                "status": "failed",
                "error": f"unusable destination {item.dest!r}: {exc}",
                "timestamp": utcnow(),
                "source": "poll",
                "pollMessageId": item.message_id or None,
            },
        )
        return

    text = item.text[:MAX_TEXT]
    try:
        await _send_and_store(chat_id, text, source="poll", poll_message_id=item.message_id)
    except OpenWAError as exc:
        log.error("poll: send to %s failed - message lost from the queue: %s", chat_id, exc)
        await store.upsert_message(
            session_name=settings.openwa_session_name,
            message_id=None,
            set_fields={
                "direction": "out",
                "chatId": chat_id,
                "phone": phone_of(chat_id),
                "body": text,
                "type": "text",
                "fromMe": True,
                "status": "failed",
                "error": str(exc),
                "timestamp": utcnow(),
                "source": "poll",
                "pollMessageId": item.message_id or None,
            },
        )
        return

    poll_state.last_text[chat_id] = text
    log.info("poll -> sent to %s: %r", chat_id, text[:60])


async def _poll_once() -> None:
    # Never dequeue what cannot be delivered. An endpoint hands a message over
    # by removing it from its queue, so polling while the session is down would
    # destroy messages rather than defer them.
    if not poll_state.session_ready:
        poll_state.session_ready = await _session_is_ready()
        if not poll_state.session_ready:
            log.debug("poll: skipped, session is not ready")
            return

    result = await poll_client.poll(settings.poll_url)

    if not result.ok:
        log.warning("poll failed: %s", result.error)
        # A failure may mean the gateway went away too; re-check next time.
        poll_state.session_ready = False
        return

    if result.is_empty:
        # Proof the endpoint dequeues, which retires the consecutive-repeat rule.
        if not poll_state.endpoint_dequeues:
            log.info("poll: endpoint reported empty - repeat suppression disabled from here")
        poll_state.endpoint_dequeues = True
        return

    for item in result.messages:
        if item.message_id:
            if not await store.claim_polled(
                session_name=settings.openwa_session_name, poll_message_id=item.message_id
            ):
                log.debug("poll: already handled message id %s", item.message_id)
                continue
        elif not poll_state.endpoint_dequeues:
            try:
                chat_id = to_chat_id(item.dest, settings.default_country_code)
            except InvalidNumber:
                chat_id = item.dest
            if poll_state.last_text.get(chat_id) == item.text:
                log.debug("poll: suppressing an unchanged repeat for %s", chat_id)
                continue

        await _deliver_polled(item)


async def _poll_loop() -> None:
    log.info(
        "polling %s every %ss for messages to send",
        settings.poll_url,
        settings.poll_interval,
    )
    while True:
        await asyncio.sleep(settings.poll_interval)
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A poll loop that dies takes the whole outbound path with it.
            log.exception("poll: unhandled error, continuing")


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

        # Media arrives as a flag, not bytes. Fetch it once - the claim is
        # atomic, so a redelivered event does not download it again.
        wanted = event == msg_map.INBOUND_EVENT or settings.media_outbound
        if (
            settings.media_enabled
            and wanted
            and fields.get("hasMedia")
            and message_id
            and fields.get("chatId")
            and await store.claim_media(session_name=session_name, message_id=message_id)
        ):
            _spawn(_save_media(message_id, fields["chatId"], fields.get("contactNumber")))

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
