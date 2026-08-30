from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from wechat_bot.db.models import JSON_DOCUMENT, enum_column


class AgentRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_USER = "WAITING_USER"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AgentInboxKind(StrEnum):
    RUN_REQUEST = "RUN_REQUEST"
    QUESTION_ANSWER = "QUESTION_ANSWER"


class AgentEventType(StrEnum):
    SESSION_CREATED = "SESSION_CREATED"
    RUN_CREATED = "RUN_CREATED"
    RUN_STATUS_CHANGED = "RUN_STATUS_CHANGED"
    QUESTION_ASKED = "QUESTION_ASKED"
    QUESTION_ANSWERED = "QUESTION_ANSWERED"


class PendingQuestionStatus(StrEnum):
    PENDING = "PENDING"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AgentDefinition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_definition"
    __table_args__ = (UniqueConstraint("workspace_id", "definition_key"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    definition_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("definition_id", "version_number"),
        Index("ix_agent_version_definition_published", "definition_id", "published_at"),
        CheckConstraint("version_number >= 1", name="agent_version_number_positive"),
    )

    definition_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_definition.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    specification_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    published_by_principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_session"
    __table_args__ = (
        Index("ix_agent_session_workspace_created", "workspace_id", "created_at"),
        CheckConstraint("last_inbox_seq >= 0", name="agent_session_inbox_seq_nonnegative"),
        CheckConstraint("last_event_seq >= 0", name="agent_session_event_seq_nonnegative"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    agent_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_version.id", ondelete="RESTRICT"), nullable=False
    )
    requester_principal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="RESTRICT"), nullable=False
    )
    task_scope: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    task_scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    last_inbox_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="uq_agent_run_session_idempotency",
        ),
        UniqueConstraint(
            "session_id",
            "active_slot",
            name="uq_agent_run_session_active",
        ),
        Index("ix_agent_run_session_created", "session_id", "created_at"),
        CheckConstraint(
            "(active_slot = 1 AND status IN "
            "('QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'WAITING_USER', 'PAUSED')) "
            "OR (active_slot IS NULL AND status IN "
            "('COMPLETED', 'FAILED', 'CANCELLED', 'EXPIRED'))",
            name="agent_run_status_active_slot",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_session.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[AgentRunStatus] = mapped_column(
        enum_column(AgentRunStatus, "agent_run_status"),
        default=AgentRunStatus.QUEUED,
        nullable=False,
    )
    active_slot: Mapped[int | None] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class PendingQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_pending_question"
    __table_args__ = (
        UniqueConstraint("run_id", "open_slot"),
        Index("ix_agent_question_due", "status", "expires_at"),
        CheckConstraint(
            "(open_slot = 1 AND status = 'PENDING') OR "
            "(open_slot IS NULL AND status IN ('ANSWERED', 'EXPIRED', 'CANCELLED'))",
            name="agent_question_status_open_slot",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_session.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    allowed_principal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="RESTRICT"), nullable=False
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    status: Mapped[PendingQuestionStatus] = mapped_column(
        enum_column(PendingQuestionStatus, "agent_pending_question_status"),
        default=PendingQuestionStatus.PENDING,
        nullable=False,
    )
    open_slot: Mapped[int | None] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_by_principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="RESTRICT")
    )
    answer_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    answer_sha256: Mapped[str | None] = mapped_column(String(64))
    answer_inbox_seq: Mapped[int | None] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentSessionInbox(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_session_inbox"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        UniqueConstraint("question_id"),
        Index("ix_agent_session_inbox_run", "run_id", "seq"),
        CheckConstraint("seq >= 1", name="agent_session_inbox_seq_positive"),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_session.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[AgentInboxKind] = mapped_column(
        enum_column(AgentInboxKind, "agent_inbox_kind"), nullable=False
    )
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False
    )
    actor_principal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="RESTRICT"), nullable=False
    )
    question_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_pending_question.id", ondelete="RESTRICT"),
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AgentEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_event"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        Index("ix_agent_event_run", "run_id", "seq"),
        CheckConstraint("seq >= 1", name="agent_event_seq_positive"),
    )

    session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_session.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_run.id", ondelete="CASCADE")
    )
    question_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_pending_question.id", ondelete="SET NULL")
    )
    event_type: Mapped[AgentEventType] = mapped_column(
        enum_column(AgentEventType, "agent_event_type"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ImmutableAgentRecordError(RuntimeError):
    pass


def _reject_immutable_record_change(
    mapper: Mapper[Any],
    connection: Connection,
    target: object,
) -> None:
    del mapper, connection
    raise ImmutableAgentRecordError(f"{type(target).__name__} is append-only")


def _guard_session_identity(
    mapper: Mapper[Any],
    connection: Connection,
    target: AgentSession,
) -> None:
    del mapper, connection
    state = inspect(target)
    frozen_fields = ("workspace_id", "agent_version_id", "requester_principal_id", "task_scope")
    if any(state.attrs[field].history.has_changes() for field in frozen_fields):
        raise ImmutableAgentRecordError("AgentSession execution context is immutable")


for _immutable_model in (AgentVersion, AgentSessionInbox, AgentEvent):
    event.listen(_immutable_model, "before_update", _reject_immutable_record_change)
    event.listen(_immutable_model, "before_delete", _reject_immutable_record_change)
event.listen(AgentSession, "before_update", _guard_session_identity)
