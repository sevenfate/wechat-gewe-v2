from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from wechat_bot.db.models import JSON_DOCUMENT, enum_column


class MaiBotBridgeDirection(StrEnum):
    TO_MAIBOT = "TO_MAIBOT"
    FROM_MAIBOT = "FROM_MAIBOT"


class MaiBotBridgeKind(StrEnum):
    MESSAGE = "MESSAGE"
    REPLY = "REPLY"
    PROACTIVE = "PROACTIVE"


class MaiBotBridgeStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SENT = "SENT"
    ACKED = "ACKED"
    RECEIVED = "RECEIVED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"


class MaiBotConnectionStatus(StrEnum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    BACKOFF = "BACKOFF"


class MaiBotConnectionState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "maibot_connection_state"
    __table_args__ = (UniqueConstraint("deployment_id"),)

    deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment.id", ondelete="CASCADE"),
        nullable=False,
    )
    deployment_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    activation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_revision_activation.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[MaiBotConnectionStatus] = mapped_column(
        enum_column(MaiBotConnectionStatus, "maibot_connection_status"),
        default=MaiBotConnectionStatus.CONNECTING,
        nullable=False,
    )
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class MaiBotBridgeEnvelope(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "maibot_bridge_envelope"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "direction",
            "transport_message_id",
            name="uq_maibot_bridge_transport",
        ),
        UniqueConstraint(
            "deployment_id",
            "source_event_id",
            name="uq_maibot_bridge_source_event",
        ),
        Index("ix_maibot_bridge_due", "deployment_id", "status", "available_at"),
        Index(
            "ix_maibot_bridge_business_message",
            "deployment_id",
            "direction",
            "business_message_id",
        ),
    )

    deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment.id", ondelete="CASCADE"),
        nullable=False,
    )
    deployment_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    activation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_revision_activation.id", ondelete="CASCADE"),
        nullable=False,
    )
    bot_account_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="SET NULL")
    )
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    direction: Mapped[MaiBotBridgeDirection] = mapped_column(
        enum_column(MaiBotBridgeDirection, "maibot_bridge_direction"), nullable=False
    )
    kind: Mapped[MaiBotBridgeKind] = mapped_column(
        enum_column(MaiBotBridgeKind, "maibot_bridge_kind"), nullable=False
    )
    transport_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    business_message_id: Mapped[str | None] = mapped_column(String(600))
    reply_to_business_message_id: Mapped[str | None] = mapped_column(String(600))
    source_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("normalized_event.id", ondelete="SET NULL")
    )
    source_envelope_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("maibot_bridge_envelope.id", ondelete="SET NULL"),
    )
    actor_principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="SET NULL")
    )
    chatroom_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chatroom.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contact.id", ondelete="SET NULL")
    )
    target_wxid: Mapped[str | None] = mapped_column(String(255))
    authorization_context: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, default=dict, nullable=False
    )
    envelope: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[MaiBotBridgeStatus] = mapped_column(
        enum_column(MaiBotBridgeStatus, "maibot_bridge_status"),
        default=MaiBotBridgeStatus.PENDING,
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
