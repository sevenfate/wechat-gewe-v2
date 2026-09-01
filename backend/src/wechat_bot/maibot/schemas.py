from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from wechat_bot.db.models import ConversationType
from wechat_bot.outbox.schemas import OutboxAuthorizationContext


class MaiBotConversationContextClaims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    source_envelope_id: UUID


class MaiBotConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    websocket_url: str
    api_key: SecretStr
    client_uuid: Annotated[str, Field(min_length=1, max_length=255)]
    message_ttl_seconds: Annotated[int, Field(ge=10, le=3600)] = 300
    max_pending_messages: Annotated[int, Field(ge=1, le=10_000)] = 1000
    ack_retry_seconds: Annotated[float, Field(gt=0, le=300)] = 10.0
    reconnect_initial_seconds: Annotated[float, Field(gt=0, le=60)] = 1.0
    reconnect_max_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    enable_proactive_messages: bool = False
    tool_allowlist: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list,
        max_length=100,
    )

    @field_validator("websocket_url")
    @classmethod
    def validate_websocket_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ValueError("MaiBot URL must be an absolute ws:// or wss:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("MaiBot URL cannot contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("MaiBot URL cannot contain query parameters or a fragment")
        return normalized

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("MaiBot API key cannot be blank")
        return value

    @field_validator("tool_allowlist")
    @classmethod
    def normalize_tool_allowlist(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("MaiBot Tool allowlist cannot contain blank names")
        return list(dict.fromkeys(normalized))

    @field_validator(
        "ack_retry_seconds",
        "reconnect_initial_seconds",
        "reconnect_max_seconds",
    )
    @classmethod
    def validate_finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("MaiBot durations must be finite")
        return value

    @model_validator(mode="after")
    def validate_reconnect_window(self) -> MaiBotConnectorConfig:
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError("MaiBot reconnect maximum cannot be below its initial delay")
        return self


@dataclass(frozen=True, slots=True)
class MaiBotEventSubmission:
    workspace_id: UUID
    deployment_id: UUID
    deployment_revision_id: UUID
    bot_account_id: UUID
    bot_app_id: str
    bot_wxid: str | None
    trace_id: UUID
    event_id: UUID
    event_type: str
    conversation_type: ConversationType
    conversation_external_id: str
    actor_wxid: str
    actor_nickname: str | None
    actor_cardname: str | None
    group_name: str | None
    business_message_id: str
    occurred_at: datetime
    text: str
    authorization_context: OutboxAuthorizationContext


@dataclass(frozen=True, slots=True)
class MaiBotActivationContext:
    deployment_id: UUID
    deployment_revision_id: UUID
    activation_id: UUID
    activation_epoch: int
    fencing_token: str
    workspace_id: UUID
    plugin_id: str
    revision_grants: frozenset[str]
    revision_scope: dict[str, Any]
