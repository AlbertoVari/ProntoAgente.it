"""Application configuration sourced from environment variables."""

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    return None if value is None or not value.strip() else int(value)


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    )


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with safe local defaults."""

    database_url: str
    app_env: str
    log_level: str
    enable_legacy_v1: bool
    enable_demo_connectors: bool
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
    ai_preparation_enabled: bool
    ai_network_enabled: bool
    ai_encryption_key_id: str | None
    ai_encryption_key: str | None
    ai_openai_responses_url: str | None
    ai_openai_api_key: str | None
    ai_openai_allowed_hosts: tuple[str, ...]
    ai_openai_allowed_models: tuple[str, ...]
    ai_http_timeout_seconds: float
    ai_max_response_bytes: int
    ai_worker_lease_seconds: int
    ai_hard_max_input_tokens: int
    ai_hard_max_output_tokens: int
    ai_hard_max_run_microusd: int
    ai_hard_daily_input_tokens: int
    ai_hard_daily_output_tokens: int
    ai_hard_daily_microusd: int
    ai_fake_input_microusd_per_million: int
    ai_fake_output_microusd_per_million: int
    ai_openai_input_microusd_per_million: int | None
    ai_openai_output_microusd_per_million: int | None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read settings once per process."""

    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./prontoagente.db"),
        app_env=os.getenv("APP_ENV", "development").strip().lower(),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        enable_legacy_v1=_bool_env("ENABLE_LEGACY_V1"),
        enable_demo_connectors=_bool_env("ENABLE_DEMO_CONNECTORS"),
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
        ai_preparation_enabled=_bool_env("AI_PREPARATION_ENABLED"),
        ai_network_enabled=_bool_env("AI_NETWORK_ENABLED"),
        ai_encryption_key_id=os.getenv("AI_ENCRYPTION_KEY_ID"),
        ai_encryption_key=os.getenv("AI_ENCRYPTION_KEY"),
        ai_openai_responses_url=os.getenv("AI_OPENAI_RESPONSES_URL"),
        ai_openai_api_key=os.getenv("AI_OPENAI_API_KEY"),
        ai_openai_allowed_hosts=_csv_env(
            "AI_OPENAI_ALLOWED_HOSTS", "api.openai.com"
        ),
        ai_openai_allowed_models=_csv_env("AI_OPENAI_ALLOWED_MODELS"),
        ai_http_timeout_seconds=float(os.getenv("AI_HTTP_TIMEOUT_SECONDS", "20")),
        ai_max_response_bytes=int(os.getenv("AI_MAX_RESPONSE_BYTES", "262144")),
        ai_worker_lease_seconds=int(os.getenv("AI_WORKER_LEASE_SECONDS", "60")),
        ai_hard_max_input_tokens=int(
            os.getenv("AI_HARD_MAX_INPUT_TOKENS", "4096")
        ),
        ai_hard_max_output_tokens=int(
            os.getenv("AI_HARD_MAX_OUTPUT_TOKENS", "1024")
        ),
        ai_hard_max_run_microusd=int(
            os.getenv("AI_HARD_MAX_RUN_MICROUSD", "100000")
        ),
        ai_hard_daily_input_tokens=int(
            os.getenv("AI_HARD_DAILY_INPUT_TOKENS", "1000000")
        ),
        ai_hard_daily_output_tokens=int(
            os.getenv("AI_HARD_DAILY_OUTPUT_TOKENS", "250000")
        ),
        ai_hard_daily_microusd=int(
            os.getenv("AI_HARD_DAILY_MICROUSD", "1000000")
        ),
        ai_fake_input_microusd_per_million=int(
            os.getenv("AI_FAKE_INPUT_MICROUSD_PER_MILLION", "0")
        ),
        ai_fake_output_microusd_per_million=int(
            os.getenv("AI_FAKE_OUTPUT_MICROUSD_PER_MILLION", "0")
        ),
        ai_openai_input_microusd_per_million=_optional_int_env(
            "AI_OPENAI_INPUT_MICROUSD_PER_MILLION"
        ),
        ai_openai_output_microusd_per_million=_optional_int_env(
            "AI_OPENAI_OUTPUT_MICROUSD_PER_MILLION"
        ),
    )
