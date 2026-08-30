from __future__ import annotations

import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fingerprint_token(token: str) -> str:
    return hash_token(token)[:16]


def hash_context(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secrets_equal(first: str, second: str) -> bool:
    return secrets.compare_digest(first.encode("utf-8"), second.encode("utf-8"))
