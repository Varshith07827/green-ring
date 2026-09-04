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
