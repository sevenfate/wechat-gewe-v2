import pytest
from pydantic import ValidationError

from wechat_bot.core.config import Environment, Settings


def test_production_rejects_sqlite() -> None:
    with pytest.raises(ValidationError, match="require PostgreSQL"):
        Settings(environment=Environment.PRODUCTION, master_key="test-key")


def test_webhook_deadline_must_be_below_gewe_limit() -> None:
    with pytest.raises(ValidationError, match="below GeWe's 3 second limit"):
        Settings(webhook_ack_deadline_seconds=3)


@pytest.mark.parametrize("deadline", [0, -1, float("nan"), float("inf")])
def test_webhook_deadline_must_be_positive_and_finite(deadline: float) -> None:
    with pytest.raises(ValidationError, match="greater than zero"):
        Settings(webhook_ack_deadline_seconds=deadline)


def test_webhook_body_limit_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="body size must be greater than zero"):
        Settings(webhook_max_body_bytes=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("directory_contacts_cache_poll_attempts", -1),
        ("directory_contacts_cache_poll_interval_seconds", 0),
        ("directory_contacts_cache_poll_interval_seconds", float("nan")),
    ],
)
def test_invalid_directory_cache_poll_settings_are_rejected(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_invalid_base_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"absolute HTTP\(S\) URL"):
        Settings(public_base_url="localhost:8000")


def test_log_level_is_normalized_and_validated() -> None:
    assert Settings(log_level="debug").log_level == "DEBUG"

    with pytest.raises(ValidationError, match="unsupported log level"):
        Settings(log_level="verbose")


def test_production_rejects_non_postgresql_database() -> None:
    with pytest.raises(ValidationError, match="require PostgreSQL"):
        Settings(
            environment=Environment.PRODUCTION,
            database_url="mysql+aiomysql://localhost/wechat",
            master_key="test-key",
            public_base_url="https://bot.example.com",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sender_poll_interval_seconds", 0),
        ("sender_request_timeout_seconds", float("nan")),
        ("sender_max_concurrent_accounts", 0),
        ("sender_per_minute_limit", -1),
        ("sender_target_interval_seconds", -0.1),
        ("sender_retry_jitter_ratio", 1),
    ],
)
def test_invalid_sender_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_sender_setting_ranges_are_consistent() -> None:
    with pytest.raises(ValidationError, match="group maximum"):
        Settings(
            sender_group_interval_min_seconds=5,
            sender_group_interval_max_seconds=2,
        )
    with pytest.raises(ValidationError, match="backoff maximum"):
        Settings(sender_backoff_base_seconds=10, sender_backoff_max_seconds=2)
    with pytest.raises(ValidationError, match="lease must cover"):
        Settings(sender_lease_seconds=79, sender_request_timeout_seconds=20)
