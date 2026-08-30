from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utc_now
from wechat_bot.db.models import JSON_DOCUMENT, enum_column


class PrincipalType(StrEnum):
    ADMIN_USER = "ADMIN_USER"
    CONTACT = "CONTACT"
    GROUP_MEMBER = "GROUP_MEMBER"
    CONNECTOR = "CONNECTOR"
    PLUGIN = "PLUGIN"
    TASK_AGENT = "TASK_AGENT"
    AUTOMATION = "AUTOMATION"
    SYSTEM = "SYSTEM"


class AclScopeType(StrEnum):
    WORKSPACE = "WORKSPACE"
    BOT_ACCOUNT = "BOT_ACCOUNT"
    CHATROOM = "CHATROOM"
    CONTACT = "CONTACT"


class AclResourceType(StrEnum):
    CATEGORY = "CATEGORY"
    PLUGIN = "PLUGIN"
    COMMAND = "COMMAND"
    TOOL = "TOOL"
    AGENT = "AGENT"
    CAPABILITY = "CAPABILITY"


class AclEffect(StrEnum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class Principal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "principal"
    __table_args__ = (
        UniqueConstraint("workspace_id", "principal_type", "external_id"),
        Index("ix_principal_workspace_active", "workspace_id", "active"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[PrincipalType] = mapped_column(
        enum_column(PrincipalType, "principal_type"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)


class AclPolicyState(TimestampMixin, Base):
    __tablename__ = "acl_policy_state"

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AclRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "acl_rule"
    __table_args__ = (
        Index(
            "ix_acl_rule_lookup",
            "workspace_id",
            "resource_type",
            "resource_id",
            "revoked_at",
        ),
        Index("ix_acl_rule_scope", "workspace_id", "scope_type", "scope_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    principal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("principal.id", ondelete="CASCADE")
    )
    scope_type: Mapped[AclScopeType] = mapped_column(
        enum_column(AclScopeType, "acl_scope_type"), nullable=False
    )
    scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[AclResourceType] = mapped_column(
        enum_column(AclResourceType, "acl_resource_type"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    effect: Mapped[AclEffect] = mapped_column(enum_column(AclEffect, "acl_effect"), nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    membership_epoch: Mapped[int | None] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(255))


class PolicyDecision(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "policy_decision"
    __table_args__ = (Index("ix_policy_decision_trace_created", "trace_id", "created_at"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    effect: Mapped[AclEffect] = mapped_column(
        enum_column(AclEffect, "policy_decision_effect"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    request_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    matched_rule_ids: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
