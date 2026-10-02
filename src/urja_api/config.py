from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (prefix ``URJA_``) or `.env`."""

    # hide_input_in_errors: a start-up error about a bad value must not print a secret.
    model_config = SettingsConfigDict(env_prefix="URJA_", env_file=".env", extra="ignore", hide_input_in_errors=True)

    portal_base_url: str = "https://urja-ops.flockenergy.tech"
    portal_email: str = Field(min_length=1, description="Portal login e-mail")
    portal_password: SecretStr = Field(min_length=1, description="Portal login password")
    # One HTTP request to the portal; well under portal_max_wait_s, so a retry still fits.
    portal_timeout_s: float = 10.0
    # The portal allows ~120 requests per 60 s across all its /portal/* JSON endpoints;
    # stay comfortably below that so other users of the same account aren't starved.
    portal_rate_limit_per_minute: int = 100
    portal_rate_limit_burst: int = 10
    # Longest one portal call may take, login, throttling and retries included, before the
    # API answers 503. With a cached copy to fall back on, a readings refresh gets 3 s at
    # most. (A reference sync makes several calls, each with this budget.)
    portal_max_wait_s: float = 25.0

    db_path: Path = Path("data/urja.sqlite3")
    # Reference data (meters, hierarchy, locations, transformers) is re-synced this often.
    sync_interval_s: int = 900
    # A meter's readings are refetched from the portal when older than this.
    readings_ttl_s: int = 900
    # Prefetch every meter's readings in the background (enables fleet-wide consumption and
    # anomaly queries; one pass takes ~4-5 minutes at the default rate limit), and repeat the
    # pass this often. The data changes rarely, so hourly keeps portal load low.
    warm_readings: bool = True
    warm_interval_s: int = 3600

    # Optional shared secret for this API. When set, requests must send `X-API-Key`.
    api_key: SecretStr | None = None

    @field_validator("api_key")
    @classmethod
    def _usable_api_key(cls, key: SecretStr | None) -> SecretStr | None:
        """A blank key means "no key"; anything else must survive an HTTP header and a browser."""
        if key is None or not key.get_secret_value().strip():
            return None
        if not all(0x21 <= ord(c) <= 0x7E for c in key.get_secret_value()):
            raise ValueError("URJA_API_KEY must be printable ASCII without spaces")
        return key


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
