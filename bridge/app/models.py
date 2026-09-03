"""Request/response schemas for the bridge API."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SendRequest(BaseModel):
    """The Postman payload: {"id": "<phone with country code>", "msg": "<text>"}.

    `number`/`to`/`phone` and `message`/`text`/`body` are accepted as aliases so
    an existing client does not have to be rewritten.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., description="Phone number with country code, e.g. 919876543210")
    msg: str = Field(..., min_length=1, max_length=4096, description="Message text")

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


class HealthResponse(BaseModel):
    ok: bool
    mongo: bool
    openwa: bool
    session: dict[str, Any] | None = None
    detail: str | None = None
