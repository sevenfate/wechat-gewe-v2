from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from wechat_bot.db.models import JSON_DOCUMENT, enum_column


class ToolCallStatus(StrEnum):
    """Durable outcome of one brokered Tool call."""

    RECEIVED = "RECEIVED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ToolInvocationMode(StrEnum):
    USER_REQUESTED = "USER_REQUESTED"
    AUTONOMOUS = "AUTONOMOUS"


class ToolCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-oriented-enough ledger for connector and agent Tool calls.

    The broker keeps the opaque context only as a digest.  The encrypted context
    itself remains in the MaiBot bridge envelope, so a database read cannot be
    used to mint a new caller identity.
    """

    __tablename__ = "tool_call"
    __table_args__ = (
        UniqueConstraint(
            "connector_revision_id",
            "external_tool_call_id",
            name="uq_tool_call_connector_external_id",
        ),
        Index("ix_tool_call_workspace_created", "workspace_id", "created_at"),
        Index("ix_tool_call_status_available", "status", "available_at"),
        Index("ix_tool_call_trace", "trace_id", "created_at"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    connector_deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plugin_deployment.id", ondelete="RESTRICT"), nullable=False
    )
    connector_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connector_activation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_revision_activation.id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_deployment_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plugin_deployment.id", ondelete="SET NULL")
    )
    target_revision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plugin_deployment_revision.id", ondelete="SET NULL")
    )
    target_activation_epoch: Mapped[int | None] = mapped_column(Integer)
    external_tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    connector_context_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(160), nullable=False)
    tool_schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    invocation_mode: Mapped[ToolInvocationMode] = mapped_column(
        enum_column(ToolInvocationMode, "tool_invocation_mode"), nullable=False
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    arguments_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    bot_account_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="SET NULL")
    )
    chatroom_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chatroom.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contact.id", ondelete="SET NULL")
    )
    status: Mapped[ToolCallStatus] = mapped_column(
        enum_column(ToolCallStatus, "tool_call_status"),
        default=ToolCallStatus.RECEIVED,
        nullable=False,
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
