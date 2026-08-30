from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator

from wechat_bot.db.models import CallbackManagementMode, ConnectionStatus


class ConnectionCreate(BaseModel):
    workspace_slug: Annotated[str, Field(min_length=1, max_length=80)] = "default"
    workspace_name: Annotated[str, Field(min_length=1, max_length=120)] = "默认工作区"
    name: Annotated[str, Field(min_length=1, max_length=120)]
    api_base_url: AnyHttpUrl
    token: SecretStr

    @field_validator("workspace_slug", "workspace_name", "name")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("api_base_url")
    @classmethod
    def reject_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("API base URL cannot contain credentials")
        return value


class ConnectionTokenUpdate(BaseModel):
    token: SecretStr


class ConnectionModeUpdate(BaseModel):
    callback_mode: CallbackManagementMode


class ConnectionStatusUpdate(BaseModel):
    status: ConnectionStatus


class ConnectionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    api_base_url: str
    token_fingerprint: str
    callback_mode: CallbackManagementMode
    callback_url: str
    callback_expected_url: str | None
    callback_verified_at: datetime | None
    last_callback_at: datetime | None
    last_callback_error: str | None
    status: ConnectionStatus
    created_at: datetime
    updated_at: datetime


class ConnectionList(BaseModel):
    items: list[ConnectionView]
    total: int


class CallbackApplyResult(BaseModel):
    connection: ConnectionView
    applied: bool
