from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from wechat_bot.db.tool_models import ToolCallStatus, ToolInvocationMode


class ToolCallRequest(BaseModel):
    """Versioned request accepted from MaiBot or another trusted runtime."""

    model_config = ConfigDict(extra="forbid")

    bridge_version: Literal["1.0"] = "1.0"
    external_tool_call_id: Annotated[str, Field(min_length=1, max_length=255)]
    connector_context_id: Annotated[str, Field(min_length=1, max_length=255)]
    deployment_revision_id: UUID
    activation_epoch: Annotated[int, Field(ge=1)]
    tool_name: Annotated[str, Field(min_length=1, max_length=160)]
    tool_schema_version: Annotated[str, Field(min_length=1, max_length=40)] = "1.0"
    arguments: dict[str, Any] = Field(default_factory=dict)
    invocation_mode: ToolInvocationMode = ToolInvocationMode.USER_REQUESTED
    deadline_at: datetime

    @field_validator("external_tool_call_id", "connector_context_id", "tool_name")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier cannot be blank")
        return normalized

    @field_validator("tool_schema_version")
    @classmethod
    def normalize_schema_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tool schema version cannot be blank")
        return normalized

    @field_validator("deadline_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_user_context(self) -> ToolCallRequest:
        if (
            self.invocation_mode is ToolInvocationMode.USER_REQUESTED
            and not self.connector_context_id
        ):
            raise ValueError("user-requested calls require connector context")
        return self


class ToolCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    tool_schema_version: str
    plugin_id: str
    plugin_name: str
    deployment_id: UUID
    revision_id: UUID
    description: str
    effect_class: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    required_capabilities: list[str]


class ToolCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_version: Literal["1.0"] = "1.0"
    items: list[ToolCatalogItem]


class ToolCallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    connector_deployment_id: UUID
    connector_revision_id: UUID
    connector_activation_id: UUID
    target_deployment_id: UUID | None
    target_revision_id: UUID | None
    target_activation_epoch: int | None
    external_tool_call_id: str
    connector_context_digest: str
    tool_name: str
    tool_schema_version: str
    invocation_mode: ToolInvocationMode
    arguments: dict[str, Any]
    arguments_sha256: str
    trace_id: UUID
    actor_principal_id: UUID | None
    bot_account_id: UUID | None
    chatroom_id: UUID | None
    contact_id: UUID | None
    status: ToolCallStatus
    result: dict[str, Any] | None
    error_code: str | None
    error_detail: str | None
    deadline_at: datetime
    available_at: datetime
    attempt_count: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ToolCallResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_version: Literal["1.0"] = "1.0"
    call: ToolCallView


class ToolCallListResponse(BaseModel):
    items: list[ToolCallView]
    total: int


class ToolCatalogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bridge_version: Literal["1.0"] = "1.0"
    deployment_revision_id: UUID
    activation_epoch: Annotated[int, Field(ge=1)]
    connector_context_id: Annotated[str, Field(min_length=1, max_length=255)]
