"""Application configuration sourced from environment variables."""

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with safe local defaults."""

    database_url: str
    app_env: str
    log_level: str
    enable_legacy_v1: bool
    api_key_pepper: str
    m365_connector_enabled: bool
    m365_platform_tenant_id: str | None
    m365_permission_attestation: str | None
    m365_tenant_id: str | None
    m365_client_id: str | None
    m365_client_secret: str | None
    m365_mailbox_id: str | None
    m365_folder_id: str | None
    http_timeout_seconds: float
    outbox_lease_seconds: int
    outbox_max_attempts: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once per process."""

    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./prontoagente.db"),
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        enable_legacy_v1=_bool_env("ENABLE_LEGACY_V1"),
        api_key_pepper=os.getenv("API_KEY_PEPPER", "dev-only-change-me"),
        m365_connector_enabled=_bool_env("M365_CONNECTOR_ENABLED"),
        m365_platform_tenant_id=os.getenv("M365_PLATFORM_TENANT_ID"),
        m365_permission_attestation=os.getenv("M365_PERMISSION_ATTESTATION"),
        m365_tenant_id=os.getenv("M365_TENANT_ID"),
        m365_client_id=os.getenv("M365_CLIENT_ID"),
        m365_client_secret=os.getenv("M365_CLIENT_SECRET"),
        m365_mailbox_id=os.getenv("M365_MAILBOX_ID"),
        m365_folder_id=os.getenv("M365_FOLDER_ID"),
        http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "10")),
        outbox_lease_seconds=int(os.getenv("OUTBOX_LEASE_SECONDS", "30")),
        outbox_max_attempts=int(os.getenv("OUTBOX_MAX_ATTEMPTS", "3")),
    )
