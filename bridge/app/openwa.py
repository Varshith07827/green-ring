"""Thin async client for the local OpenWA gateway."""

import logging
import re
from typing import Any
from urllib.parse import unquote

import httpx

log = logging.getLogger("bridge.openwa")


class OpenWAError(RuntimeError):
    """An OpenWA call failed. `status` is the upstream HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class OpenWAClient:
    def __init__(self, base_url: str, api_key: str, session_name: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.session_name = session_name
        self._session_id: str | None = None
        self._phone_cache: dict[str, str | None] = {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    # -- plumbing -----------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise OpenWAError(f"cannot reach OpenWA at {self.base_url}: {exc}") from exc

        if response.status_code >= 400:
            try:
                body = response.json()
                detail = body.get("message") or body.get("error") or body
            except ValueError:
                body = response.text
                detail = body
            raise OpenWAError(
                f"OpenWA {method} {path} failed with {response.status_code}: {detail}",
                status=response.status_code,
                body=body,
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    # -- sessions -----------------------------------------------------------

    async def session_id(self, refresh: bool = False) -> str:
        """Resolve the configured session NAME to the uuid the API routes use."""
        if self._session_id and not refresh:
            return self._session_id

        sessions = await self._request("GET", "/api/sessions")
        if not isinstance(sessions, list):
            raise OpenWAError(f"unexpected /api/sessions response: {sessions!r}")

        for session in sessions:
            if session.get("name") == self.session_name:
                self._session_id = session["id"]
                return self._session_id

        known = ", ".join(s.get("name", "?") for s in sessions) or "none"
        raise OpenWAError(
            f"no OpenWA session named '{self.session_name}' (existing sessions: {known}). "
            f"Restart the bridge to create it, or create it in the dashboard with that exact name."
        )

    async def create_session(self) -> dict:
        return await self._request("POST", "/api/sessions", json={"name": self.session_name})

    async def ensure_session(self) -> dict:
        """Create the configured session if the gateway does not have it yet.

        Done at startup so the dashboard always has a session waiting to be
        started and scanned, rather than needing one created by hand under
        exactly the right name.
        """
        sessions = await self._request("GET", "/api/sessions")
        for session in sessions if isinstance(sessions, list) else []:
            if session.get("name") == self.session_name:
                self._session_id = session["id"]
                return {"action": "found", "session": session}

        created = await self.create_session()
        self._session_id = created["id"]
        return {"action": "created", "session": created}

    async def start_session(self) -> Any:
        return await self._request("POST", f"/api/sessions/{await self.session_id()}/start")

    async def session_info(self) -> dict:
        return await self._request("GET", f"/api/sessions/{await self.session_id()}")

    async def qr(self) -> dict:
        return await self._request("GET", f"/api/sessions/{await self.session_id()}/qr")

    # -- messages -----------------------------------------------------------

    async def _post_message(self, endpoint: str, payload: dict) -> dict:
        """POST to one of the session's message endpoints, surviving a recreated session."""
        session_id = await self.session_id()
        path = f"/api/sessions/{session_id}/messages/{endpoint}"
        try:
            return await self._request("POST", path, json=payload)
        except OpenWAError as exc:
            # A stale cached session id (session recreated) resolves on retry.
            if exc.status == 404:
                await self.session_id(refresh=True)
                return await self._request(
                    "POST",
                    f"/api/sessions/{await self.session_id()}/messages/{endpoint}",
                    json=payload,
                )
            raise

    async def send_text(self, chat_id: str, text: str) -> dict:
        return await self._post_message("send-text", {"chatId": chat_id, "text": text})

    async def send_media(
        self,
        kind: str,
        chat_id: str,
        *,
        url: str | None = None,
        data_base64: str | None = None,
        mimetype: str | None = None,
        filename: str | None = None,
        caption: str | None = None,
        ptt: bool = False,
    ) -> dict:
        """Send one file as `kind` - image, video, audio, document or sticker.

        Exactly one of `url` (the gateway fetches it) or `data_base64` (we send
        the bytes) carries the file. The gateway prefers base64 when both are
        present, so passing both is a way to send something other than what you
        meant; callers pick one.
        """
        payload: dict[str, Any] = {"chatId": chat_id}
        if data_base64:
            payload["base64"] = data_base64
            # The gateway documents mimetype as required alongside base64: with
            # no type it cannot choose a container and the send fails.
            payload["mimetype"] = mimetype or "application/octet-stream"
        elif url:
            payload["url"] = url
            if mimetype:
                payload["mimetype"] = mimetype
        else:
            raise OpenWAError("send_media needs either url or data_base64")

        if filename:
            payload["filename"] = filename
        if caption:
            payload["caption"] = caption
        if kind == "audio" and ptt:
            payload["ptt"] = True

        return await self._post_message(f"send-{kind}", payload)

    # -- the rest of the send family ----------------------------------------
    # Thin on purpose: each one is the gateway's own contract with the chat id
    # already resolved. Anything that needs deciding is decided in main.py, so
    # these stay readable next to the API docs they mirror.

    async def reply(self, chat_id: str, quoted_message_id: str, text: str) -> dict:
        return await self._post_message(
            "reply", {"chatId": chat_id, "quotedMessageId": quoted_message_id, "text": text}
        )

    async def react(self, chat_id: str, message_id: str, emoji: str) -> dict:
        # An empty emoji is not a missing value here - it is how a reaction is removed.
        return await self._post_message(
            "react", {"chatId": chat_id, "messageId": message_id, "emoji": emoji}
        )

    async def forward(self, from_chat_id: str, to_chat_id: str, message_id: str) -> dict:
        return await self._post_message(
            "forward",
            {"fromChatId": from_chat_id, "toChatId": to_chat_id, "messageId": message_id},
        )

    async def send_location(
        self,
        chat_id: str,
        latitude: float,
        longitude: float,
        description: str | None = None,
        address: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "chatId": chat_id,
            "latitude": latitude,
            "longitude": longitude,
        }
        if description:
            payload["description"] = description
        if address:
            payload["address"] = address
        return await self._post_message("send-location", payload)

    async def send_contact(self, chat_id: str, name: str, number: str) -> dict:
        return await self._post_message(
            "send-contact",
            {"chatId": chat_id, "contactName": name, "contactNumber": number},
        )

    async def send_poll(self, chat_id: str, name: str, options: list[str]) -> dict:
        return await self._post_message(
            "send-poll", {"chatId": chat_id, "name": name, "options": options}
        )

    async def edit_message(self, chat_id: str, message_id: str, body: str) -> dict:
        return await self._post_message(
            "edit", {"chatId": chat_id, "messageId": message_id, "body": body}
        )

    async def delete_message(self, chat_id: str, message_id: str, for_everyone: bool) -> dict:
        return await self._post_message(
            "delete",
            {"chatId": chat_id, "messageId": message_id, "forEveryone": for_everyone},
        )

    async def star_message(self, chat_id: str, message_id: str, star: bool) -> dict:
        return await self._post_message(
            "star", {"chatId": chat_id, "messageId": message_id, "star": star}
        )

    async def pin_message(
        self, chat_id: str, message_id: str, duration_seconds: int | None = None
    ) -> dict:
        payload: dict[str, Any] = {"chatId": chat_id, "messageId": message_id}
        if duration_seconds:
            payload["durationSeconds"] = duration_seconds
        return await self._post_message("pin", payload)

    async def unpin_message(self, chat_id: str, message_id: str) -> dict:
        return await self._post_message("unpin", {"chatId": chat_id, "messageId": message_id})

    async def contact_phone(self, contact_id: str) -> str | None:
        """Resolve a privacy id (`@lid`) to a phone number, best-effort.

        WhatsApp increasingly identifies people by an `@lid` rather than their
        number, and an outbound message carries no `senderPhone` - the sender is
        you, so the gateway's inbound-only resolution does not help. This is the
        on-demand equivalent.

        Answers are cached, including the misses: an `@lid` the account has
        never seen resolves to null and would otherwise be looked up again on
        every message from that chat.
        """
        if contact_id in self._phone_cache:
            return self._phone_cache[contact_id]

        session_id = await self.session_id()
        try:
            result = await self._request(
                "GET", f"/api/sessions/{session_id}/contacts/{contact_id}/phone"
            )
        except OpenWAError as exc:
            log.debug("could not resolve %s to a phone: %s", contact_id, exc)
            return None

        phone = (result or {}).get("phone")
        phone = phone.strip() if isinstance(phone, str) and phone.strip() else None
        # Bounded, so a long-running process with many contacts cannot grow
        # without limit.
        if len(self._phone_cache) < 5000:
            self._phone_cache[contact_id] = phone
        return phone

    async def download_media(self, chat_id: str, message_id: str) -> tuple[bytes, str, str | None]:
        """Fetch a message's media bytes.

        Returns (content, mimetype, filename). The gateway serves its archived
        copy first and falls back to the inline copy on the message row, so this
        works for inbound media whether or not archiving is switched on.

        A `404` here is normal rather than exceptional: the message may carry no
        media, the bytes may have exceeded the gateway's size cap when it was
        stored, or it may have been a URL-based send, which stores nothing.
        """
        session_id = await self.session_id()
        path = f"/api/sessions/{session_id}/messages/{chat_id}/{message_id}/media"
        try:
            response = await self._client.get(path)
        except httpx.RequestError as exc:
            raise OpenWAError(f"cannot reach OpenWA at {self.base_url}: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:200]
            raise OpenWAError(
                f"media download failed with {response.status_code}: {detail}",
                status=response.status_code,
            )

        mimetype = response.headers.get("content-type", "application/octet-stream")
        filename = None
        disposition = response.headers.get("content-disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
        if match:
            filename = unquote(match.group(1)).strip() or None

        return response.content, mimetype, filename

    # -- webhooks -----------------------------------------------------------

    async def list_webhooks(self) -> list[dict]:
        result = await self._request("GET", f"/api/sessions/{await self.session_id()}/webhooks")
        return result if isinstance(result, list) else []

    async def ensure_webhook(self, url: str, events: list[str], secret: str | None) -> dict:
        """Register (or realign) the bridge's own webhook on this session.

        `secret` is write-only upstream, so it is re-sent on every update rather
        than compared.
        """
        session_id = await self.session_id()
        body: dict[str, Any] = {"url": url, "events": events}
        if secret:
            body["secret"] = secret

        for hook in await self.list_webhooks():
            if hook.get("url") == url:
                if set(hook.get("events") or []) == set(events) and not secret:
                    return {"action": "kept", "webhook": hook}
                updated = await self._request(
                    "PUT", f"/api/sessions/{session_id}/webhooks/{hook['id']}", json=body
                )
                return {"action": "updated", "webhook": updated}

        created = await self._request("POST", f"/api/sessions/{session_id}/webhooks", json=body)
        return {"action": "created", "webhook": created}
