"""Translate OpenWA webhook payloads into the stored message shape."""

from datetime import datetime, timezone
from typing import Any

from .numbers import phone_of

INBOUND_EVENT = "message.received"
OUTBOUND_EVENT = "message.sent"


def _to_datetime(epoch_seconds: Any) -> datetime | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def message_fields(event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Fields for a `message.received` / `message.sent` delivery.

    OpenWA reports `from` as the chat and `to` as this account for inbound, and
    the reverse for outbound, so the counterparty is picked by direction.
    """
    direction = "in" if event == INBOUND_EVENT else "out"
    sender = data.get("from")
    recipient = data.get("to")
    chat_id = data.get("chatId") or (sender if direction == "in" else recipient)

    return {
        "messageId": data.get("id"),
        "direction": direction,
        "chatId": chat_id,
        "phone": phone_of(chat_id),
        "from": sender,
        "to": recipient,
        "body": data.get("body"),
        "type": data.get("type"),
        "kind": data.get("kind"),
        "isGroup": data.get("isGroup"),
        "hasMedia": data.get("hasMedia"),
        "fromMe": direction == "out",
        "senderPhone": data.get("senderPhone"),
        "contact": data.get("contact"),
        "timestamp": _to_datetime(data.get("timestamp")),
        "source": "webhook",
        "raw": data,
    }


def ack_fields(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """(messageId, status) from a `message.ack` / `message.failed` delivery."""
    return data.get("messageId") or data.get("id"), data.get("status")


def revoked_target(data: dict[str, Any]) -> str | None:
    """The id of the message a `message.revoked` delivery refers to.

    Reconcile on `revokedId` first: on whatsapp-web.js `id` is the revocation
    notification, a different message that will not match anything stored.
    """
    return data.get("revokedId") or data.get("id")
