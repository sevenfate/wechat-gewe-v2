from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class BootstrapOwnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        if len(password.encode("utf-8")) > 1_024:
            raise ValueError("password is too long")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("password cannot be empty")
        if len(value.get_secret_value().encode("utf-8")) > 1_024:
            raise ValueError("password is too long")
        return value


class AuthUserResponse(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    roles: list[str]
    permissions: list[str]


class LoginResponse(BaseModel):
    user: AuthUserResponse
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


class CsrfResponse(BaseModel):
    csrf_token: str


class MessageResponse(BaseModel):
    message: str
