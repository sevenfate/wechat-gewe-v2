from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now


class AdminUserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class LoginThrottleDimension(StrEnum):
    ACCOUNT = "ACCOUNT"
    SOURCE = "SOURCE"


class AuthEventOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    DENIED = "DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    REVOKED = "REVOKED"


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda values: [item.value for item in values],
    )


class AdminUser(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "admin_user"
    __table_args__ = (
        CheckConstraint("auth_version >= 1", name="auth_version_positive"),
        Index("ix_admin_user_status", "status"),
    )

    username: Mapped[str] = mapped_column(String(80), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[AdminUserStatus] = mapped_column(
        enum_column(AdminUserStatus, "admin_user_status"),
        default=AdminUserStatus.ACTIVE,
        nullable=False,
    )
    auth_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RbacRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "rbac_role"

    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class RbacPermission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "rbac_permission"

    code: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500))


class RbacUserRole(Base):
    __tablename__ = "rbac_user_role"

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("admin_user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("rbac_role.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RbacRolePermission(Base):
    __tablename__ = "rbac_role_permission"

    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("rbac_role.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("rbac_permission.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AdminSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "admin_session"
    __table_args__ = (
        Index("ix_admin_session_user_active", "user_id", "revoked_at"),
        Index("ix_admin_session_expiry", "idle_expires_at", "absolute_expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(120))
    source_key_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent_hash: Mapped[str | None] = mapped_column(String(64))


class AuthBootstrapState(Base):
    __tablename__ = "auth_bootstrap_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("admin_user.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    token_fingerprint: Mapped[str] = mapped_column(String(16), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuthLoginThrottle(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "auth_login_throttle"
    __table_args__ = (
        UniqueConstraint("dimension", "key_hash"),
        Index("ix_auth_login_throttle_blocked", "blocked_until"),
    )

    dimension: Mapped[LoginThrottleDimension] = mapped_column(
        enum_column(LoginThrottleDimension, "login_throttle_dimension"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthSecurityEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "auth_security_event"
    __table_args__ = (Index("ix_auth_security_event_created", "created_at"),)

    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    outcome: Mapped[AuthEventOutcome] = mapped_column(
        enum_column(AuthEventOutcome, "auth_event_outcome"), nullable=False
    )
    user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    session_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_session.id", ondelete="SET NULL")
    )
    username_normalized: Mapped[str | None] = mapped_column(String(80))
    source_key_hash: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
