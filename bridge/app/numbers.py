"""Phone number -> WhatsApp chat id normalisation."""

import re

DIGITS_ONLY = re.compile(r"[^\d]")
WA_SUFFIXES = ("@c.us", "@g.us", "@s.whatsapp.net", "@lid", "@newsletter")

MIN_DIGITS = 8
MAX_DIGITS = 15  # E.164 hard limit


class InvalidNumber(ValueError):
    """Raised when an `id` cannot be turned into a WhatsApp chat id."""


def to_chat_id(raw: str, default_country_code: str = "") -> str:
    """Turn user input into a WhatsApp chat id.

    Accepts ``+62 812-3456-789``, ``0062812...``, ``628123456789`` or an
    already-formed jid such as ``628123456789@c.us`` / ``120363...@g.us``
    (passed through untouched, so groups keep working).
    """
    if not isinstance(raw, str):
        raise InvalidNumber("id must be a string")

    value = raw.strip()
    if not value:
        raise InvalidNumber("id is empty")

    # Already a jid - trust it, only normalise the case of the suffix.
    lowered = value.lower()
    for suffix in WA_SUFFIXES:
        if lowered.endswith(suffix):
            local = value[: -len(suffix)]
            if not local:
                raise InvalidNumber(f"'{raw}' has no id before '{suffix}'")
            return f"{local}{suffix}"

    digits = DIGITS_ONLY.sub("", value)
    if not digits:
        raise InvalidNumber(f"'{raw}' contains no digits")

    # 00 is the international access prefix; strip it before the country code.
    if digits.startswith("00"):
        digits = digits[2:]

    if default_country_code:
        # Already international if it leads with the country code and is long
        # enough to carry a subscriber number behind it; otherwise treat the
        # input as national - drop any trunk-prefix zero and prepend the code.
        already_international = digits.startswith(default_country_code) and len(digits) >= 10
        if not already_international:
            digits = default_country_code + digits.lstrip("0")

    if len(digits) < MIN_DIGITS:
        raise InvalidNumber(
            f"'{raw}' has only {len(digits)} digits - send the full number "
            f"including the country code (e.g. 919876543210)"
        )
    if len(digits) > MAX_DIGITS:
        raise InvalidNumber(f"'{raw}' has {len(digits)} digits, more than the E.164 maximum of {MAX_DIGITS}")

    return f"{digits}@c.us"


def phone_of(chat_id: str | None) -> str | None:
    """The bare digits of a 1:1 chat id, or None for groups / non-numeric jids."""
    if not chat_id or "@" not in chat_id:
        return None
    local, _, domain = chat_id.partition("@")
    if domain.lower() not in ("c.us", "s.whatsapp.net"):
        return None
    return local if local.isdigit() else None
