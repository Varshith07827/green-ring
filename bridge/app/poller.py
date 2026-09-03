"""Polling your server for messages to send - the pull half of the bridge.

Every POLL_INTERVAL seconds:

    GET  <POLL_URL>                 Authorization: Bearer <token>
    <-   {"id": "919876543210", "msg": "Hello"}
    ->   sent to 919876543210 through OpenWA

Nothing ever connects inward, which is the whole point: a PC anywhere can queue
a message on your server, and it goes out from this machine without this machine
being reachable from the internet.

**`id` is the destination here, not a dedup key.** In the system this replaces,
`id` in the response body WAS the dedup key (`external_id = obj.get("id")`), and
the destination came from which per-chat URL was polled. Carrying that over
would mean the first message to a number sends and every later message to that
same number is silently dropped as a duplicate. So the dedup id is read from
`_id` / `message_id` / `messageId` / `external_id` / `externalId` / `uid`, and
never from `id`.

Deduplication, in order:

1. **An explicit message id is authoritative.** Seen before -> skipped, and the
   record of it lives in MongoDB so a restart does not resend the backlog.
2. **Without one, only a consecutive repeat is suppressed** - the same text sent
   to the same chat twice in a row. A poll URL states what is pending, so an
   unchanged answer means "nothing new".
3. **An endpoint that has ever answered empty is exempt from rule 2.** Answering
   empty proves it dequeues, and a dequeuing endpoint never hands the same
   message over twice - so everything it gives is new, however the text reads.

Rule 3 matters because rule 2 does real damage to a dequeuing endpoint:
suppressing a message there does not defer it, it destroys it - the endpoint
already removed it from its queue to hand it over.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("bridge.poller")

# Field names a queued message may carry its text under.
TEXT_KEYS = ("msg", "message", "text", "content", "body", "reply")
# ...its destination under. `id` first: that is the documented field.
DEST_KEYS = ("id", "to", "number", "phone", "chatId", "chat_id", "recipient")
# ...and its own identity under. Deliberately NOT `id` - see the module docstring.
MESSAGE_ID_KEYS = ("_id", "message_id", "messageId", "external_id", "externalId", "uid")
# Envelopes the list of messages may be nested inside.
ENVELOPE_KEYS = ("data", "result", "payload", "messages", "items", "queue")
# Protocol metadata. Their presence does not by itself disqualify an object -
# a real message may carry a `status` - but they identify a status envelope
# when the object also has no recipient, which sharpens the warning.
PROTOCOL_KEYS = ("success", "error", "ok", "code")

BODY_EXCERPT = 2000


@dataclass(frozen=True)
class Outgoing:
    """One message the poll found waiting."""

    text: str
    dest: str = ""
    message_id: str = ""


@dataclass(frozen=True)
class PollResult:
    ok: bool
    status_code: int = 0
    messages: tuple[Outgoing, ...] = ()
    body: str = ""
    error: str = ""

    @property
    def is_empty(self) -> bool:
        """A successful poll that offered nothing - proof the endpoint dequeues."""
        return self.ok and not self.messages


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return ""


def _first_str(obj: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def parse(body: str) -> list[Outgoing]:
    """Everything a poll response is offering to send.

    Understands one object, an array of objects, and either nested under a
    `data`/`result`/`messages`-style envelope. An array yields *every* message
    in it - answering a burst with three objects means three messages, and
    quietly delivering one of them is how a backlog disappears.

    **Nothing without a destination is ever sent.** That single rule is what
    stops an endpoint's own chatter from being relayed to a stranger: a status
    body like `{"success":true,"message":"No messages"}` has a `message` field
    and no recipient, so it is refused rather than delivered as the words "No
    messages". A message must say who it is for.
    """
    text = (body or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        # Not JSON, so it names no destination and cannot be a message. An HTML
        # body is worth calling out by name: it means the URL is serving a page
        # - a login screen or an error page - rather than answering as an API.
        looks_like_html = text[:1] == "<" or text[:15].lower().startswith("<!doctype")
        log.warning(
            "poll response is not JSON%s - ignoring: %r",
            " (it looks like an HTML page)" if looks_like_html else "",
            text[:120],
        )
        return []
    return _from_json(parsed)


def _from_json(parsed: Any, depth: int = 0) -> list[Outgoing]:
    if depth > 3:
        return []

    if isinstance(parsed, list):
        found: list[Outgoing] = []
        for item in parsed:
            found.extend(_from_json(item, depth + 1))
        return found

    if not isinstance(parsed, dict):
        # A bare string names nobody, so it cannot be addressed.
        return []

    text = ""
    for key in TEXT_KEYS:
        if key in parsed:
            text = _text_of(parsed[key])
            if text:
                break

    dest = _first_str(parsed, DEST_KEYS)
    if text and dest:
        return [Outgoing(text=text, dest=dest, message_id=_first_str(parsed, MESSAGE_ID_KEYS))]

    # Text with no recipient is not a message. Try the envelopes before giving
    # up, so a real message wrapped in a status envelope - the common
    # `{"success":true,"message":"OK","data":{...}}` shape - is still found.
    for key in ENVELOPE_KEYS:
        if key in parsed:
            nested = _from_json(parsed[key], depth + 1)
            if nested:
                return nested

    if text:
        envelope = any(key in parsed for key in PROTOCOL_KEYS)
        log.warning(
            "poll response carries text but no destination - refusing to send it%s: %r",
            " (it looks like a status envelope, not a message)" if envelope else "",
            text[:80],
        )
    return []


class PollClient:
    def __init__(self, token: str = "", timeout: float = 15.0):
        self._token = (token or "").strip()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain",
            "User-Agent": "openwa-bridge/1.1.0",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def poll(self, url: str) -> PollResult:
        """One GET. Deliberately not retried: the next poll is the retry, and
        hammering an endpoint that is already struggling helps nobody."""
        try:
            response = await self._client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return PollResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        body = response.text
        if not (200 <= response.status_code < 300):
            return PollResult(
                ok=False,
                status_code=response.status_code,
                body=body[:BODY_EXCERPT],
                error=f"HTTP {response.status_code}",
            )

        return PollResult(
            ok=True,
            status_code=response.status_code,
            messages=tuple(parse(body)),
            body=body[:BODY_EXCERPT],
        )
