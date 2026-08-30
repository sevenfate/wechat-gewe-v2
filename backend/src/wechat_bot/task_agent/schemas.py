from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from wechat_bot.db.agent_models import (
    AgentEventType,
    AgentInboxKind,
    AgentRunStatus,
    PendingQuestionStatus,
)


class AgentDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    definition_key: Annotated[str, Field(min_length=1, max_length=120)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=1000)] = ""

    @field_validator("definition_key", "name")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized


class AgentVersionPublish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specification: dict[str, Any]
    published_by_principal_id: UUID | None = None


class AgentVersionPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specification: dict[str, Any]


class AgentSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    agent_version_id: UUID
    requester_principal_id: UUID
    task_scope: dict[str, Any] = Field(default_factory=dict)


class AgentSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    agent_version_id: UUID
    task_scope: dict[str, Any] = Field(default_factory=dict)


class AgentRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: Annotated[str, Field(min_length=1, max_length=255)]
    input_payload: dict[str, Any]

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("idempotency_key cannot be blank")
        return normalized


class AgentRunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AgentRunStatus
    reason: Annotated[str | None, Field(max_length=500)] = None
    error_code: Annotated[str | None, Field(max_length=100)] = None


class PendingQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_principal_id: UUID
    prompt: Annotated[str, Field(min_length=1, max_length=4000)]
    context: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt cannot be blank")
        return normalized


class PendingQuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: UUID
    answer_payload: dict[str, Any]


class PendingQuestionOverrideAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_payload: dict[str, Any]
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason cannot be blank")
        return normalized


class AgentContextView(BaseModel):
    workspace_id: UUID
    workspace_name: str


class AgentDefinitionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    definition_key: str
    name: str
    description: str
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentDefinitionList(BaseModel):
    items: list[AgentDefinitionView]
    total: int


class AgentVersionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    definition_id: UUID
    version_number: int
    specification: dict[str, Any]
    specification_sha256: str
    published_by_principal_id: UUID | None
    published_at: datetime

    @field_serializer("specification")
    def serialize_specification(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)


class AgentVersionList(BaseModel):
    items: list[AgentVersionView]
    total: int


class AgentSessionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_version_id: UUID
    requester_principal_id: UUID
    task_scope: dict[str, Any]
    task_scope_sha256: str
    last_inbox_seq: int
    last_event_seq: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("task_scope")
    def serialize_task_scope(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)


class AgentSessionList(BaseModel):
    items: list[AgentSessionView]
    total: int


class AgentRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    idempotency_key: str
    input_payload: dict[str, Any]
    input_sha256: str
    status: AgentRunStatus
    started_at: datetime | None
    finished_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("input_payload")
    def serialize_input_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)


class AgentRunList(BaseModel):
    items: list[AgentRunView]
    total: int


class AgentSessionInboxView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    seq: int
    kind: AgentInboxKind
    run_id: UUID
    actor_principal_id: UUID
    question_id: UUID | None
    payload: dict[str, Any]
    payload_sha256: str
    created_at: datetime

    @field_serializer("payload")
    def serialize_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)


class AgentEventView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    seq: int
    run_id: UUID | None
    question_id: UUID | None
    event_type: AgentEventType
    payload: dict[str, Any]
    created_at: datetime

    @field_serializer("payload")
    def serialize_payload(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)


class PendingQuestionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    session_id: UUID
    run_id: UUID
    allowed_principal_id: UUID
    prompt: str
    context: dict[str, Any]
    status: PendingQuestionStatus
    expires_at: datetime
    answered_at: datetime | None
    answered_by_principal_id: UUID | None
    answer_payload: dict[str, Any] | None
    answer_sha256: str | None
    answer_inbox_seq: int | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("context")
    def serialize_context(self, value: dict[str, Any]) -> dict[str, Any]:
        return _without_private_reasoning(value)

    @field_serializer("answer_payload")
    def serialize_answer_payload(
        self,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return _without_private_reasoning(value) if value is not None else None


class QuestionAnswerResult(BaseModel):
    question: PendingQuestionView
    inbox_item: AgentSessionInboxView
    run: AgentRunView


class AgentSessionStateView(BaseModel):
    session: AgentSessionView
    active_run: AgentRunView | None
    inbox: list[AgentSessionInboxView]
    events: list[AgentEventView]
    questions: list[PendingQuestionView]
    inbox_has_more: bool = False
    events_has_more: bool = False
    questions_has_more: bool = False


_PRIVATE_REASONING_KEYS = frozenset(
    {
        "analysis",
        "chainofthought",
        "hiddenreasoning",
        "hiddenthoughts",
        "internalreasoning",
        "reasoning",
        "reasoningcontent",
        "reasoningdetails",
        "scratchpad",
        "thinking",
    }
)


def _without_private_reasoning(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _without_private_reasoning_value(item)
        for key, item in value.items()
        if not is_private_reasoning_key(key)
    }


def _without_private_reasoning_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _without_private_reasoning(value)
    if isinstance(value, list | tuple):
        return [_without_private_reasoning_value(item) for item in value]
    return value


def is_private_reasoning_key(key: str) -> bool:
    normalized = "".join(
        character for character in key.strip().casefold() if character not in {"-", "_"}
    )
    return normalized in _PRIVATE_REASONING_KEYS
