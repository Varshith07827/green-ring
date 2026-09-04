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

    async def send_text(self, chat_id: str, text: str) -> dict:
        session_id = await self.session_id()
        payload = {"chatId": chat_id, "text": text}
        try:
            return await self._request(
                "POST", f"/api/sessions/{session_id}/messages/send-text", json=payload
            )
        except OpenWAError as exc:
            # A stale cached session id (session recreated) resolves on retry.
            if exc.status == 404:
                await self.session_id(refresh=True)
                return await self._request(
                    "POST",
                    f"/api/sessions/{await self.session_id()}/messages/send-text",
                    json=payload,
                )
            raise

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
