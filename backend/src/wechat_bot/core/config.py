from __future__ import annotations

import math
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        env_prefix="WECHAT_BOT_",
        extra="ignore",
    )

    app_name: str = "GeWe 微信机器人管理平台"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    database_url: str = "sqlite+aiosqlite:///../.data/wechat-bot.db"
    database_echo: bool = False

    public_base_url: str = "http://127.0.0.1:8000"
    gewe_api_base_url: str = "http://api.geweapi.com"
    master_key: SecretStr | None = None
    local_master_key_path: Path = Path("../.data/master.key")

    webhook_max_body_bytes: int = 1_048_576
    webhook_ack_deadline_seconds: float = 2.5

    sender_poll_interval_seconds: float = 0.25
    sender_max_concurrent_accounts: int = 8
    sender_per_minute_limit: int = 40
    sender_target_interval_seconds: float = 1.0
    sender_group_interval_min_seconds: float = 2.0
    sender_group_interval_max_seconds: float = 5.0
    sender_max_attempts: int = 5
    sender_backoff_base_seconds: float = 2.0
    sender_backoff_max_seconds: float = 60.0
    sender_retry_jitter_ratio: float = 0.2
    sender_lease_seconds: float = 90.0
    sender_request_timeout_seconds: float = 20.0
    sender_offline_retry_seconds: float = 30.0

    auth_bootstrap_token: SecretStr | None = None
    auth_session_idle_seconds: int = 1_800
    auth_session_absolute_seconds: int = 43_200
    auth_login_window_seconds: int = 900
    auth_login_max_failures: int = 5
    auth_login_block_seconds: int = 900

    @field_validator("public_base_url", "gewe_api_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base URL must be an absolute HTTP(S) URL")
        return normalized

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("webhook_max_body_bytes")
    @classmethod
    def validate_webhook_max_body_bytes(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("webhook maximum body size must be greater than zero")
        return value

    @field_validator("webhook_ack_deadline_seconds")
    @classmethod
    def validate_webhook_ack_deadline(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value >= 3:
            raise ValueError(
                "webhook ACK deadline must be greater than zero and remain below "
                "GeWe's 3 second limit"
            )
        return value

    @field_validator(
        "sender_poll_interval_seconds",
        "sender_backoff_base_seconds",
        "sender_backoff_max_seconds",
        "sender_lease_seconds",
        "sender_request_timeout_seconds",
        "sender_offline_retry_seconds",
    )
    @classmethod
    def validate_positive_sender_duration(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("sender durations must be finite and greater than zero")
        return value

    @field_validator(
        "sender_max_concurrent_accounts",
        "sender_per_minute_limit",
        "sender_max_attempts",
    )
    @classmethod
    def validate_positive_sender_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("sender limits must be greater than zero")
        return value

    @field_validator(
        "sender_target_interval_seconds",
        "sender_group_interval_min_seconds",
        "sender_group_interval_max_seconds",
    )
    @classmethod
    def validate_nonnegative_sender_interval(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("sender intervals must be finite and nonnegative")
        return value

    @field_validator("sender_retry_jitter_ratio")
    @classmethod
    def validate_sender_jitter_ratio(cls, value: float) -> float:
        if not math.isfinite(value) or not 0 <= value < 1:
            raise ValueError("sender retry jitter ratio must be in [0, 1)")
        return value

    @field_validator(
        "auth_session_idle_seconds",
        "auth_session_absolute_seconds",
        "auth_login_window_seconds",
        "auth_login_max_failures",
        "auth_login_block_seconds",
    )
    @classmethod
    def validate_positive_auth_setting(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("authentication limits and durations must be greater than zero")
        return value

    @field_validator("auth_bootstrap_token")
    @classmethod
    def validate_bootstrap_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("bootstrap token must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_deployment_safety(self) -> Settings:
        if self.auth_session_idle_seconds >= self.auth_session_absolute_seconds:
            raise ValueError("session idle timeout must be shorter than its absolute lifetime")
        if self.sender_group_interval_max_seconds < self.sender_group_interval_min_seconds:
            raise ValueError("sender group maximum interval cannot be below its minimum")
        if self.sender_backoff_max_seconds < self.sender_backoff_base_seconds:
            raise ValueError("sender backoff maximum cannot be below its base")
        if self.sender_lease_seconds < 60 + self.sender_request_timeout_seconds:
            raise ValueError("sender lease must cover the rate-limit window and request timeout")
        if self.environment in {Environment.STAGING, Environment.PRODUCTION}:
            if not self.database_url.lower().startswith("postgresql"):
                raise ValueError("staging and production require PostgreSQL")
            if self.master_key is None or not self.master_key.get_secret_value():
                raise ValueError("WECHAT_BOT_MASTER_KEY is required outside local development")
            if not self.public_base_url.startswith("https://"):
                raise ValueError("staging and production require an HTTPS public base URL")
        return self

    @property
    def is_local(self) -> bool:
        return self.environment in {Environment.DEVELOPMENT, Environment.TEST}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
