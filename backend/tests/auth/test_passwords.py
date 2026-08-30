import pytest

from wechat_bot.auth.passwords import PasswordManager


def test_passwords_use_argon2id_and_verify() -> None:
    manager = PasswordManager()
    encoded = manager.hash("a sufficiently long password")

    assert encoded.startswith("$argon2id$")
    assert manager.verify(encoded, "a sufficiently long password")
    assert not manager.verify(encoded, "wrong password")
    assert "sufficiently long" not in encoded


def test_short_password_is_rejected() -> None:
    manager = PasswordManager()

    with pytest.raises(ValueError, match="at least 12"):
        manager.hash("too-short")


def test_invalid_password_hash_fails_closed() -> None:
    manager = PasswordManager()

    assert not manager.verify("not-an-argon2-hash", "any password")
    assert manager.needs_rehash("not-an-argon2-hash")
