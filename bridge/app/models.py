"""Request/response schemas for the bridge API."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# What `type` may name. "voice" is audio sent as a WhatsApp voice note (ptt),
# which is a flag on the audio endpoint rather than an endpoint of its own.
SEND_KINDS = {"image", "video", "audio", "voice", "document", "sticker"}


MEDIA_CAPTION_MAX = 1024  # the gateway's cap on a caption, shorter than a text body


class SendRequest(BaseModel):
    """The Postman payload: {"id": "<phone with country code>", "msg": "<text>"}.

    Add `media` to send a file instead, with `msg` becoming its caption:

        {"id": "...", "msg": "on the roof", "media": "https://example.com/x.jpg"}

    `number`/`to`/`phone` and `message`/`text`/`body` are accepted as aliases so
    an existing client does not have to be rewritten.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Phone number with country code, e.g. 919876543210")
    msg: str | None = Field(
        None,
        max_length=4096,
        description="Message text, or the caption when sending media",
    )
    media: str | None = Field(
        None,
        description="An http(s) URL, a data: URI, or raw base64 for the file to send",
    )
    filename: str | None = Field(None, max_length=255, description="Filename shown for documents")
    mimetype: str | None = Field(None, max_length=255, description="Required with raw base64")
    type: str | None = Field(
        None,
        description="Force the kind: image, video, audio, voice, document or sticker. "
        "Derived from the mimetype when omitted.",
    )

    @model_validator(mode="after")
    def _check_caption_and_type(self) -> "SendRequest":
        # Whether the request has *anything* to send is checked by the caller,
        # not here: a multipart send carries its file outside this model, so a
        # rule written here would reject the uploads it cannot see.
        if self.media and self.msg and len(self.msg) > MEDIA_CAPTION_MAX:
            raise ValueError(
                f"a caption is at most {MEDIA_CAPTION_MAX} characters "
                f"(got {len(self.msg)}); send the text as its own message"
            )
        if self.type and self.type.lower() not in SEND_KINDS:
            raise ValueError(f"type must be one of: {', '.join(sorted(SEND_KINDS))}")
        return self

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "id" not in data:
            for alias in ("number", "to", "phone", "chatId"):
                if data.get(alias):
                    data["id"] = data[alias]
                    break
        if "msg" not in data:
            for alias in ("message", "text", "body"):
                if data.get(alias):
                    data["msg"] = data[alias]
                    break
        return data


class SendResponse(BaseModel):
    ok: bool
    messageId: str | None = None
    chatId: str
    status: str
    storedId: str | None = None
    type: str = "text"
    mediaPath: str | None = None


class HealthResponse(BaseModel):
    ok: bool
    mongo: bool
    openwa: bool
    session: dict[str, Any] | None = None
    detail: str | None = None


# --------------------------------------------------------------------------
# The rest of the send family
#
# Every one of these names the chat as `id`, exactly as POST /send does - a
# phone number or a group jid, never the gateway's chatId form. The bridge
# converts. `messageId` is the WhatsApp id of the message being acted on, which
# is what /send and /messages both hand back.
# --------------------------------------------------------------------------

ID_ALIASES = ("number", "to", "phone", "chatId", "chat")
MESSAGE_ALIASES = ("msgId", "message_id", "messageID", "waMessageId")


def _alias(data: Any, target: str, aliases: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return data
    data = dict(data)
    if target not in data or data.get(target) in (None, ""):
        for name in aliases:
            if data.get(name):
                data[target] = data[name]
                break
    return data


class ChatRequest(BaseModel):
    """Anything addressed to a chat."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Phone number with country code, or a group jid")

    @model_validator(mode="before")
    @classmethod
    def _ids(cls, data: Any) -> Any:
        return _alias(data, "id", ID_ALIASES)


class MessageRequest(ChatRequest):
    """Anything acting on one existing message in a chat."""

    messageId: str = Field(..., description="The WhatsApp message id, as returned by /send")

    @model_validator(mode="before")
    @classmethod
    def _message_ids(cls, data: Any) -> Any:
        return _alias(_alias(data, "id", ID_ALIASES), "messageId", MESSAGE_ALIASES)


class ReplyRequest(MessageRequest):
    msg: str = Field(..., min_length=1, max_length=4096, description="The reply text")

    @model_validator(mode="before")
    @classmethod
    def _reply(cls, data: Any) -> Any:
        data = _alias(_alias(data, "id", ID_ALIASES), "messageId", MESSAGE_ALIASES + ("quotedMessageId", "replyTo"))
        return _alias(data, "msg", ("message", "text", "body"))


class ReactRequest(MessageRequest):
    emoji: str = Field(
        ...,
        max_length=64,
        description="The emoji to react with. An empty string removes the reaction.",
    )


class ForwardRequest(MessageRequest):
    """`id` is where it goes; `from` is the chat it is already in."""

    from_: str = Field(..., alias="from", description="The chat the message is currently in")

    @model_validator(mode="before")
    @classmethod
    def _forward(cls, data: Any) -> Any:
        data = _alias(_alias(data, "id", ID_ALIASES), "messageId", MESSAGE_ALIASES)
        return _alias(data, "from", ("fromChatId", "fromId", "source"))


class LocationRequest(ChatRequest):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    description: str | None = Field(None, max_length=1024)
    address: str | None = Field(None, max_length=1024)

    @model_validator(mode="before")
    @classmethod
    def _location(cls, data: Any) -> Any:
        data = _alias(data, "id", ID_ALIASES)
        data = _alias(data, "latitude", ("lat",))
        return _alias(data, "longitude", ("lng", "lon", "long"))


class ContactRequest(ChatRequest):
    """A contact card. `name` and `number` are the card's, not the recipient's."""

    name: str = Field(..., min_length=1, max_length=255, description="Name on the contact card")
    number: str = Field(..., min_length=1, max_length=32, description="Number on the contact card")

    @model_validator(mode="before")
    @classmethod
    def _contact(cls, data: Any) -> Any:
        data = _alias(data, "id", ID_ALIASES)
        data = _alias(data, "name", ("contactName",))
        return _alias(data, "number", ("contactNumber",))


class PollRequest(ChatRequest):
    question: str = Field(..., min_length=1, max_length=255)
    options: list[str] = Field(..., description="Between 2 and 12 answers, WhatsApp's own limit")

    @model_validator(mode="before")
    @classmethod
    def _poll(cls, data: Any) -> Any:
        data = _alias(data, "id", ID_ALIASES)
        return _alias(data, "question", ("name", "title", "msg", "text"))

    @model_validator(mode="after")
    def _option_count(self) -> "PollRequest":
        cleaned = [o.strip() for o in self.options if o and o.strip()]
        if not 2 <= len(cleaned) <= 12:
            raise ValueError(f"a poll needs between 2 and 12 options (got {len(cleaned)})")
        self.options = cleaned
        return self


class EditRequest(MessageRequest):
    msg: str = Field(..., min_length=1, max_length=4096, description="The replacement text")

    @model_validator(mode="before")
    @classmethod
    def _edit(cls, data: Any) -> Any:
        data = _alias(_alias(data, "id", ID_ALIASES), "messageId", MESSAGE_ALIASES)
        return _alias(data, "msg", ("message", "text", "body"))


class DeleteRequest(MessageRequest):
    forEveryone: bool = Field(
        False,
        description="True deletes it for everyone in the chat; false only for you",
    )


class StarRequest(MessageRequest):
    star: bool = Field(True, description="False un-stars it")


class PinRequest(MessageRequest):
    durationSeconds: int | None = Field(
        None, description="86400 (24h), 604800 (7d) or 2592000 (30d). Defaults to 24h."
    )


class ActionResponse(BaseModel):
    ok: bool
    chatId: str
    action: str
    messageId: str | None = None
    status: str | None = None
    storedId: str | None = None
