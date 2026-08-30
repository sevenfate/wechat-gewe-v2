from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wechat_bot.db.models import OutboxStatus
from wechat_bot.db.policy_models import AclResourceType


class OutboxAuthorizationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    workspace_id: UUID
    deployment_id: UUID
    deployment_revision_id: UUID
    actor_principal_id: UUID | None = None
    chatroom_id: UUID | None = None
    contact_id: UUID | None = None
    resource_type: AclResourceType
    resource_id: Annotated[str, Field(min_length=1, max_length=255)]
    parent_plugin_id: Annotated[str | None, Field(max_length=255)] = None

    @model_validator(mode="after")
    def require_exactly_one_conversation(self) -> OutboxAuthorizationContext:
        if (self.chatroom_id is None) == (self.contact_id is None):
            raise ValueError("authorization context must bind exactly one conversation")
        return self


class OutboxMessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_account_id: UUID
    trace_id: UUID
    idempotency_key: str
    action_type: str
    target_wxid: str
    payload: dict[str, object]
    payload_sha256: str
    status: OutboxStatus
    priority: int
    available_at: datetime
    expires_at: datetime | None
    attempt_count: int
    last_error_code: str | None
    last_attempt_started_at: datetime | None
    last_attempt_finished_at: datetime | None
    provider_message_id: str | None
    provider_new_message_id: str | None
    provider_create_time: int | None
    provider_message_type: int | None
    created_at: datetime
    updated_at: datetime


class OutboxMessageList(BaseModel):
    items: list[OutboxMessageView]
    total: int


class OutboxManualActionRequest(BaseModel):
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason cannot be blank")
        return normalized


class OutboxReconcileRequest(OutboxManualActionRequest):
    resolution: Annotated[
        OutboxStatus,
        Field(description="Only SENT or FAILED_FINAL is accepted for UNKNOWN messages"),
    ]

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: OutboxStatus) -> OutboxStatus:
        if value not in {OutboxStatus.SENT, OutboxStatus.FAILED_FINAL}:
            raise ValueError("resolution must be SENT or FAILED_FINAL")
        return value


class TextOutboxPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=10_000)]
    at_wxids: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        default_factory=list,
        max_length=100,
    )

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be blank")
        return value

    @field_validator("at_wxids")
    @classmethod
    def normalize_at_wxids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            stripped = value.strip()
            if not stripped or "," in stripped:
                raise ValueError("mention wxid must be nonblank and cannot contain commas")
            if stripped not in seen:
                normalized.append(stripped)
                seen.add(stripped)
        return normalized

    @model_validator(mode="after")
    def validate_visible_mentions(self) -> TextOutboxPayload:
        if self.at_wxids and "@" not in self.text:
            raise ValueError("text must contain a visible @ mention when at_wxids is set")
        return self
