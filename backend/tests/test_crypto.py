from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from wechat_bot.core.config import Environment, Settings
from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError


def test_local_cipher_persists_generated_key(tmp_path: Path) -> None:
    key_path = tmp_path / "secrets" / "master.key"
    settings = Settings(environment=Environment.TEST, local_master_key_path=key_path)

    first = CredentialCipher.from_settings(settings)
    ciphertext = first.encrypt("gewe-token")
    second = CredentialCipher.from_settings(settings)

    assert second.decrypt(ciphertext) == "gewe-token"
    assert key_path.exists()


def test_configured_key_is_used() -> None:
    key = Fernet.generate_key().decode("ascii")
    cipher = CredentialCipher.from_settings(Settings(master_key=key))

    assert cipher.decrypt(cipher.encrypt("secret")) == "secret"


def test_wrong_key_returns_safe_error() -> None:
    ciphertext = CredentialCipher(Fernet.generate_key()).encrypt("secret")

    with pytest.raises(CredentialDecryptionError, match="cannot be decrypted"):
        CredentialCipher(Fernet.generate_key()).decrypt(ciphertext)


def test_empty_credential_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        CredentialCipher(Fernet.generate_key()).encrypt("")


def test_invalid_configured_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="valid Fernet key"):
        CredentialCipher.from_settings(Settings(master_key="not-a-fernet-key"))


def test_fingerprint_is_stable_and_does_not_expose_secret() -> None:
    fingerprint = CredentialCipher.fingerprint("gewe-token")

    assert fingerprint == CredentialCipher.fingerprint("gewe-token")
    assert len(fingerprint) == 16
    assert "gewe-token" not in fingerprint
