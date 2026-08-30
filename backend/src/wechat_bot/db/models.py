from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class CallbackManagementMode(StrEnum):
    MANUAL = "MANUAL"
    PLATFORM_MANAGED = "PLATFORM_MANAGED"


class ConnectionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class BotAccountStatus(StrEnum):
    UNBOUND = "UNBOUND"
    QR_PENDING = "QR_PENDING"
    SCANNED = "SCANNED"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    RECONNECTING = "RECONNECTING"
    NEED_QR = "NEED_QR"
    DISABLED = "DISABLED"


class InboxStatus(StrEnum):
    RECEIVED = "RECEIVED"
    NORMALIZED = "NORMALIZED"
    DISPATCHING = "DISPATCHING"
    DISPATCHED = "DISPATCHED"
    FAILED = "FAILED"
    IGNORED_SELF = "IGNORED_SELF"


class ConversationType(StrEnum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    SYSTEM = "SYSTEM"
    UNKNOWN = "UNKNOWN"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda values: [item.value for item in values],
    )


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspace"
    __table_args__ = (
        CheckConstraint("singleton_key = 1", name="singleton_key_one"),
        UniqueConstraint("singleton_key"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    singleton_key: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )


class GeweConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "gewe_connection"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    api_base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    callback_mode: Mapped[CallbackManagementMode] = mapped_column(
        enum_column(CallbackManagementMode, "callback_management_mode"),
        default=CallbackManagementMode.MANUAL,
        nullable=False,
    )
    callback_secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    callback_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    callback_expected_url_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    callback_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_callback_error: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[ConnectionStatus] = mapped_column(
        enum_column(ConnectionStatus, "gewe_connection_status"),
        default=ConnectionStatus.ACTIVE,
        nullable=False,
    )


class BotAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_account"
    __table_args__ = (
        UniqueConstraint("gewe_connection_id", "app_id"),
        Index("ix_bot_account_connection_status", "gewe_connection_id", "status"),
    )

    gewe_connection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("gewe_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    wxid: Mapped[str | None] = mapped_column(String(255))
    alias: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(String(500))
    pending_login_uuid: Mapped[str | None] = mapped_column(String(255))
    qr_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_error: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[BotAccountStatus] = mapped_column(
        enum_column(BotAccountStatus, "bot_account_status"),
        default=BotAccountStatus.UNBOUND,
        nullable=False,
    )
    logged_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookInbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = (
        UniqueConstraint("provider", "gewe_connection_id", "app_id", "dedup_key"),
        Index(
            "ix_webhook_inbox_status_received",
            "status",
            "dispatch_available_at",
            "created_at",
        ),
    )

    provider: Mapped[str] = mapped_column(String(32), default="gewe", nullable=False)
    gewe_connection_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("gewe_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    app_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    new_msg_id: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[InboxStatus] = mapped_column(
        enum_column(InboxStatus, "webhook_inbox_status"),
        default=InboxStatus.RECEIVED,
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(1000))
    dispatch_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class NormalizedEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "normalized_event"
    __table_args__ = (
        UniqueConstraint("webhook_inbox_id"),
        Index("ix_normalized_event_conversation", "bot_account_id", "conversation_id"),
    )

    webhook_inbox_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("webhook_inbox.id", ondelete="CASCADE"), nullable=False
    )
    bot_account_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    conversation_type: Mapped[ConversationType] = mapped_column(
        enum_column(ConversationType, "conversation_type"),
        default=ConversationType.UNKNOWN,
        nullable=False,
    )
    conversation_id: Mapped[str | None] = mapped_column(String(255))
    actor_wxid: Mapped[str | None] = mapped_column(String(255))
    to_wxid: Mapped[str | None] = mapped_column(String(255))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    is_self: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    raw_ref: Mapped[str] = mapped_column(String(500), nullable=False)


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact"
    __table_args__ = (UniqueConstraint("bot_account_id", "external_id"),)

    bot_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="CASCADE"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(255))
    remark: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chatroom(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chatroom"
    __table_args__ = (UniqueConstraint("bot_account_id", "chatroom_id"),)

    bot_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="CASCADE"), nullable=False
    )
    chatroom_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    owner_wxid: Mapped[str | None] = mapped_column(String(255))
    member_count: Mapped[int | None] = mapped_column(Integer)
    discovered_from: Mapped[str] = mapped_column(String(32), nullable=False)
    placeholder: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatroomMembership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chatroom_membership"
    __table_args__ = (
        UniqueConstraint("chatroom_id", "member_wxid", "membership_epoch"),
        Index("ix_membership_active_member", "chatroom_id", "member_wxid", "left_at"),
    )

    chatroom_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("chatroom.id", ondelete="CASCADE"), nullable=False
    )
    member_wxid: Mapped[str] = mapped_column(String(255), nullable=False)
    membership_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))
    inviter_wxid: Mapped[str | None] = mapped_column(String(255))
    member_flag: Mapped[int | None] = mapped_column(BigInteger)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "outbox_message"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index("ix_outbox_status_available", "status", "available_at"),
    )

    bot_account_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bot_account.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_wxid: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    authorization_context: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus, "outbox_status"),
        default=OutboxStatus.PENDING,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    provider_new_message_id: Mapped[str | None] = mapped_column(String(255))
    provider_create_time: Mapped[int | None] = mapped_column(BigInteger)
    provider_message_type: Mapped[int | None] = mapped_column(Integer)


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_event"
    __table_args__ = (Index("ix_audit_event_trace_created", "trace_id", "created_at"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
