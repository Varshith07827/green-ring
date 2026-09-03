"""Configuration for the OpenWA bridge, loaded from bridge/.env."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

PLACEHOLDERS = {"", "change-me", "changeme", "replace-me", "your-key-here"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Bridge HTTP server -------------------------------------------------
    bridge_host: str = "0.0.0.0"
    bridge_port: int = 8000
    bridge_api_key: str = ""
    log_level: str = "info"

    # --- MongoDB ------------------------------------------------------------
    mongo_uri: str = ""
    mongo_db: str = "openwa"
    mongo_collection: str = "messages"
    mongo_events_collection: str = "events"
    store_raw_events: bool = True

    # --- OpenWA gateway -----------------------------------------------------
    openwa_base_url: str = "http://127.0.0.1:2785"
    openwa_api_key: str = ""
    openwa_session_name: str = "default"
    openwa_timeout: float = 30.0

    # --- Outbound queue polling (bridge -> your server, pull model) ---------
    # The bridge GETs this URL every POLL_INTERVAL seconds and sends whatever
    # comes back. Nothing connects inward, so a PC anywhere can queue a message
    # on your server and it goes out from here without this machine being
    # reachable. Empty disables polling entirely.
    poll_url: str = ""
    poll_token: str = ""  # sent as: Authorization: Bearer <token>
    poll_interval: float = 3.0
    poll_timeout: float = 15.0

    # --- Event ingress (OpenWA -> bridge) -----------------------------------
    # Internal plumbing on loopback. Nothing here is your endpoint.
    events_path: str = "/webhooks/openwa"
    events_secret: str = ""
    events_url: str = ""  # what OpenWA is told to POST to; defaults to localhost
    events_auto_register: bool = True
    events_subscribe: str = (
        "message.received,message.sent,message.ack,message.failed,"
        "message.revoked,message.edited,session.status"
    )

    # --- Phone number handling ---------------------------------------------
    # Optional. When set, a number short enough to look national (<= 10 digits)
    # gets this country code prefixed. Leave empty to require full E.164 input.
    default_country_code: str = ""

    @field_validator("openwa_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("events_path")
    @classmethod
    def _leading_slash(cls, v: str) -> str:
        return "/" + v.strip("/")

    @field_validator("default_country_code")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        v = v.strip().lstrip("+")
        if v and not v.isdigit():
            raise ValueError("DEFAULT_COUNTRY_CODE must be digits only, e.g. 91")
        return v

    @model_validator(mode="after")
    def _defaults_and_checks(self) -> "Settings":
        if not self.events_url:
            self.events_url = f"http://127.0.0.1:{self.bridge_port}{self.events_path}"
        return self

    @property
    def events_list(self) -> list[str]:
        return [e.strip() for e in self.events_subscribe.split(",") if e.strip()]

    @property
    def poll_enabled(self) -> bool:
        return bool(self.poll_url.strip())

    @property
    def poll_bearer(self) -> str:
        return self.poll_token.strip()

    def startup_problems(self) -> list[str]:
        """Configuration errors that should stop the process at boot."""
        problems: list[str] = []
        if self.bridge_api_key.strip().lower() in PLACEHOLDERS:
            problems.append(
                "BRIDGE_API_KEY is unset or still a placeholder. Anyone who can reach "
                "this port could send WhatsApp messages from your number."
            )
        elif len(self.bridge_api_key) < 16:
            problems.append("BRIDGE_API_KEY must be at least 16 characters.")
        if not self.mongo_uri:
            problems.append("MONGO_URI is not set.")
        if not self.openwa_api_key:
            problems.append("OPENWA_API_KEY is not set (must match API_MASTER_KEY in repo/.env).")
        if self.events_secret and len(self.events_secret) < 16:
            problems.append("EVENTS_SECRET must be at least 16 characters (OpenWA rejects shorter).")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
