"""Settings: every value comes from the environment (see .env.example)."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, case_sensitive=True
    )

    # Core
    env: Literal["dev", "test", "production"] = Field("dev", validation_alias="TRACELOOM_ENV")
    mode: Literal["lite", "full"] = Field("lite", validation_alias="TRACELOOM_MODE")
    database_url: str = Field("sqlite:///./data/traceloom.db", validation_alias="DATABASE_URL")
    secret_key: str = Field("", validation_alias="TRACELOOM_SERVER_SECRET")
    base_url: str = Field("http://localhost:8000", validation_alias="TRACELOOM_BASE_URL")
    blob_dir: str = Field("./data/blobs", validation_alias="TRACELOOM_BLOB_DIR")
    cookie_secure: bool | None = Field(None, validation_alias="TRACELOOM_COOKIE_SECURE")
    session_hours: int = Field(24 * 14, validation_alias="TRACELOOM_SESSION_HOURS")

    # Accounts
    signup_enabled: bool = Field(True, validation_alias="TRACELOOM_SIGNUP_ENABLED")
    reset_token_minutes: int = Field(60, validation_alias="TRACELOOM_RESET_TOKEN_MINUTES")
    invite_ttl_hours: int = Field(168, validation_alias="TRACELOOM_INVITE_TTL_HOURS")

    # `traceloom seed-demo` login (example.com: reserved domains like .local fail validation)
    demo_email: str = Field("demo@example.com", validation_alias="TRACELOOM_DEMO_EMAIL")
    demo_password: str = Field("demo-password-123", validation_alias="TRACELOOM_DEMO_PASSWORD")

    # Ingestion
    ingest_max_events: int = Field(1000, validation_alias="TRACELOOM_INGEST_MAX_EVENTS")
    ingest_max_bytes: int = Field(5_000_000, validation_alias="TRACELOOM_INGEST_MAX_BYTES")
    ingest_sweep_seconds: int = Field(60, validation_alias="TRACELOOM_INGEST_SWEEP_SECONDS")
    trace_window_hours: int = Field(24, validation_alias="TRACELOOM_TRACE_WINDOW_HOURS")

    # Rate limits ("<count>/<second|minute|hour>")
    rate_auth: str = Field("10/minute", validation_alias="TRACELOOM_RATE_AUTH")
    rate_ingest: str = Field("600/minute", validation_alias="TRACELOOM_RATE_INGEST")

    # Phase 2: prompts, playground, webhooks
    # Fernet key(s), newest first, comma-separated for rotation.
    encryption_key: str = Field("", validation_alias="TRACELOOM_ENCRYPTION_KEY")
    webhook_allow_private: bool = Field(False, validation_alias="TRACELOOM_WEBHOOK_ALLOW_PRIVATE")
    llm_allow_private: bool = Field(True, validation_alias="TRACELOOM_LLM_ALLOW_PRIVATE")
    playground_rate: str = Field("30/minute", validation_alias="TRACELOOM_PLAYGROUND_RATE")
    provider_timeout_seconds: int = Field(
        120, validation_alias="TRACELOOM_PROVIDER_TIMEOUT_SECONDS"
    )

    # Full mode: Redis, S3, ClickHouse
    redis_url: str = Field("redis://localhost:6379/0", validation_alias="REDIS_URL")
    s3_endpoint_url: str = Field("", validation_alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field("traceloom", validation_alias="S3_BUCKET")
    s3_access_key: str = Field("", validation_alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field("", validation_alias="S3_SECRET_KEY")
    s3_region: str = Field("us-east-1", validation_alias="S3_REGION")
    clickhouse_host: str = Field("localhost", validation_alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(8123, validation_alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field("default", validation_alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field("", validation_alias="CLICKHOUSE_PASSWORD")
    clickhouse_database: str = Field("traceloom", validation_alias="CLICKHOUSE_DATABASE")

    # Mail: console fallback when SMTP_HOST is empty
    smtp_host: str = Field("", validation_alias="SMTP_HOST")
    smtp_port: int = Field(587, validation_alias="SMTP_PORT")
    smtp_user: str = Field("", validation_alias="SMTP_USER")
    smtp_password: str = Field("", validation_alias="SMTP_PASSWORD")
    smtp_from: str = Field("traceloom@localhost", validation_alias="SMTP_FROM")
    smtp_starttls: bool = Field(True, validation_alias="SMTP_STARTTLS")

    @model_validator(mode="after")
    def _require_secret_outside_dev(self) -> Settings:
        if not self.secret_key:
            if self.env != "dev":
                raise ValueError(
                    "TRACELOOM_SERVER_SECRET must be set when TRACELOOM_ENV is not dev"
                )
            # Dev only: an ephemeral key, so sessions do not survive a restart.
            self.secret_key = secrets.token_urlsafe(32)
        return self

    @model_validator(mode="after")
    def _require_encryption_key_outside_dev(self) -> Settings:
        if not any(k.strip() for k in self.encryption_key.split(",")):
            if self.env != "dev":
                raise ValueError(
                    "TRACELOOM_ENCRYPTION_KEY must be set when TRACELOOM_ENV is not dev"
                )
            # Dev only: stored provider keys and webhook secrets become unreadable on restart.
            self.encryption_key = Fernet.generate_key().decode()
            logger.warning(
                "TRACELOOM_ENCRYPTION_KEY is not set; using an ephemeral key. "
                "Stored LLM connection keys and webhook secrets will not survive a restart."
            )
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.env == "production" if self.cookie_secure is None else self.cookie_secure


@lru_cache
def get_settings() -> Settings:
    return Settings()
