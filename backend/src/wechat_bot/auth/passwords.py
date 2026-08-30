from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_MINIMUM_PASSWORD_LENGTH = 12
_MAXIMUM_PASSWORD_BYTES = 1_024


class PasswordManager:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self._hasher.hash("dummy-password-that-is-never-valid")

    def validate_new_password(self, password: str) -> None:
        if len(password) < _MINIMUM_PASSWORD_LENGTH:
            raise ValueError(
                f"password must contain at least {_MINIMUM_PASSWORD_LENGTH} characters"
            )
        if len(password.encode("utf-8")) > _MAXIMUM_PASSWORD_BYTES:
            raise ValueError("password is too long")

    def hash(self, password: str) -> str:
        self.validate_new_password(password)
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def verify_dummy(self, password: str) -> None:
        self.verify(self._dummy_hash, password)

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except InvalidHashError:
            return True


password_manager = PasswordManager()
