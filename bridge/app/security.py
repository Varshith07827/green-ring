"""Request authentication: the bridge's own API key, and OpenWA's HMAC."""

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from .config import get_settings

log = logging.getLogger("bridge.security")


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    """Guard for every route that can send messages or read stored data.

    Two header styles carry the same `BRIDGE_API_KEY` value:

        Authorization: Bearer <key>     the documented one
        X-API-Key: <key>                still accepted

    Bearer is checked first so a client sending both is judged on the one it
    most likely meant.
    """
    expected = get_settings().bridge_api_key
    supplied = ""

    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()
    if not supplied and x_api_key:
        supplied = x_api_key.strip()

    # Compared as bytes: compare_digest rejects str with non-ASCII, and the
    # header is attacker-controlled.
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8", "replace"), expected.encode("utf-8", "replace")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid token. Send 'Authorization: Bearer <BRIDGE_API_KEY>'.",
        )


async def verify_openwa_signature(request: Request, raw_body: bytes) -> None:
    """Verify `X-OpenWA-Signature` over the exact bytes OpenWA sent.

    No secret configured means no signature is sent, so the check is skipped -
    that is only safe because this endpoint is meant to stay on localhost.
    """
    secret = get_settings().events_secret
    if not secret:
        return

    header = request.headers.get("x-openwa-signature")
    if not header:
        raise HTTPException(status_code=401, detail="Missing X-OpenWA-Signature.")

    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(header, expected):
        log.warning("rejected webhook delivery with a bad signature")
        raise HTTPException(status_code=401, detail="Bad webhook signature.")
