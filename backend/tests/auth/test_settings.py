import pytest
from pydantic import ValidationError

from wechat_bot.core.config import Settings


def test_bootstrap_token_must_be_explicit_and_high_entropy_sized() -> None:
    assert Settings().auth_bootstrap_token is None

    with pytest.raises(ValidationError, match="at least 32"):
        Settings(auth_bootstrap_token="short-token")


def test_session_idle_timeout_must_be_shorter_than_absolute_lifetime() -> None:
    with pytest.raises(ValidationError, match="idle timeout"):
        Settings(auth_session_idle_seconds=300, auth_session_absolute_seconds=300)


@pytest.mark.parametrize(
    "field_name",
    [
        "auth_session_idle_seconds",
        "auth_session_absolute_seconds",
        "auth_login_window_seconds",
        "auth_login_max_failures",
        "auth_login_block_seconds",
    ],
)
def test_authentication_durations_and_limits_must_be_positive(field_name: str) -> None:
    with pytest.raises(ValidationError, match="greater than zero"):
        Settings(**{field_name: 0})
