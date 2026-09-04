"""Translate OpenWA webhook payloads into the stored message shape."""

import re
from datetime import datetime, timezone
from typing import Any

from .numbers import phone_of

DIGITS = re.compile(r"\D+")

INBOUND_EVENT = "message.received"
OUTBOUND_EVENT = "message.sent"


def _to_datetime(epoch_seconds: Any) -> datetime | None:
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def contact_name(data: dict[str, Any]) -> str | None:
    """The most human name available for the other party.

    WhatsApp offers several, and they are not equally good. The saved contact
    name wins because it is what *you* called them; `pushName` is what they
    call themselves and changes whenever they edit their profile.
    """
    contact = data.get("contact") or {}
    for key in ("name", "shortName", "pushName", "formattedName", "verifiedName"):
        value = contact.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    push = data.get("pushName")
    return push.strip() if isinstance(push, str) and push.strip() else None


def contact_number(data: dict[str, Any], chat_id: str | None) -> str | None:
    """The other party's phone number, in digits.

    Three sources, best first: the contact record's own `number`, the
    `senderPhone` the gateway resolves for privacy-id (@lid) senders, and
    finally the chat id itself - which only carries digits for a 1:1 chat, so
    it yields nothing for groups.
    """
    contact = data.get("contact") or {}
    number = contact.get("number")
    if isinstance(number, str) and number.strip():
        return DIGITS.sub("", number) or None
    sender_phone = data.get("senderPhone")
    if isinstance(sender_phone, str) and sender_phone.strip():
        return DIGITS.sub("", sender_phone) or None
    return phone_of(chat_id)


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
        # Pulled out of `contact` so they can be queried and indexed directly,
        # rather than living behind a nested object whose shape depends on
        # which engine and which gateway flags produced it.
        "contactName": contact_name(data),
        "contactNumber": contact_number(data, chat_id),
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
