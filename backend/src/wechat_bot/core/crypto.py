from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from wechat_bot.core.config import Settings


class CredentialDecryptionError(ValueError):
    """Raised when encrypted credentials cannot be decrypted with the active key."""


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("master key must be a valid Fernet key") from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> CredentialCipher:
        configured_key = settings.master_key
        if configured_key is not None:
            return cls(configured_key.get_secret_value().encode("ascii"))

        if not settings.is_local:
            raise ValueError("a master key is required outside local development")

        return cls(_load_or_create_local_key(settings.local_master_key_path))

    def encrypt(self, plaintext: str) -> bytes:
        if not plaintext:
            raise ValueError("credential cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise CredentialDecryptionError("credential cannot be decrypted") from exc
        return plaintext.decode("utf-8")

    @staticmethod
    def fingerprint(secret: str, *, length: int = 16) -> str:
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:length]


def _load_or_create_local_key(path: Path) -> bytes:
    resolved_path = path.expanduser().resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        descriptor = os.open(resolved_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return resolved_path.read_bytes().strip()

    key = Fernet.generate_key()
    try:
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
    except BaseException:
        resolved_path.unlink(missing_ok=True)
        raise
    return key
