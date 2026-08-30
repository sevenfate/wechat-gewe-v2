from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from wechat_bot.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from wechat_bot.db.models import JSON_DOCUMENT, enum_column


class PluginPackageStatus(StrEnum):
    VERIFIED = "VERIFIED"
    AVAILABLE = "AVAILABLE"
    RETIRED = "RETIRED"
    REJECTED = "REJECTED"


class PluginDeploymentStatus(StrEnum):
    DRAFT = "DRAFT"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class PluginActivationStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    STARTING = "STARTING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class PluginEventDispatchStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"


class Plugin(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin"
    __table_args__ = (UniqueConstraint("workspace_id", "plugin_id"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PluginPackageVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin_package_version"
    __table_args__ = (
        UniqueConstraint("plugin_id", "semantic_version"),
        UniqueConstraint("package_sha256"),
    )

    plugin_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plugin.id", ondelete="CASCADE"), nullable=False
    )
    semantic_version: Mapped[str] = mapped_column(String(80), nullable=False)
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    package_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PluginPackageStatus] = mapped_column(
        enum_column(PluginPackageStatus, "plugin_package_status"),
        default=PluginPackageStatus.AVAILABLE,
        nullable=False,
    )


class PluginDeployment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin_deployment"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    plugin_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plugin.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[PluginDeploymentStatus] = mapped_column(
        enum_column(PluginDeploymentStatus, "plugin_deployment_status"),
        default=PluginDeploymentStatus.DRAFT,
        nullable=False,
    )
    active_revision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(String(500))


class PluginDeploymentRevision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin_deployment_revision"
    __table_args__ = (
        UniqueConstraint("deployment_id", "revision_number"),
        Index("ix_plugin_revision_package", "package_version_id"),
    )

    deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment.id", ondelete="CASCADE"),
        nullable=False,
    )
    package_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_package_version.id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    config_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, default=dict, nullable=False)
    grants: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, default=list, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class PluginRevisionActivation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin_revision_activation"
    __table_args__ = (
        UniqueConstraint("deployment_id", "activation_epoch"),
        Index("ix_plugin_activation_state", "deployment_id", "status"),
    )

    deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment_revision.id", ondelete="CASCADE"),
        nullable=False,
    )
    activation_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[PluginActivationStatus] = mapped_column(
        enum_column(PluginActivationStatus, "plugin_activation_status"),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_detail: Mapped[str | None] = mapped_column(String(500))


class PluginEventDispatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plugin_event_dispatch"
    __table_args__ = (
        UniqueConstraint("event_id", "deployment_id"),
        Index("ix_plugin_event_dispatch_status", "status", "updated_at"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("normalized_event.id", ondelete="CASCADE"),
        nullable=False,
    )
    deployment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plugin_deployment_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[PluginEventDispatchStatus] = mapped_column(
        enum_column(PluginEventDispatchStatus, "plugin_event_dispatch_status"),
        default=PluginEventDispatchStatus.PENDING,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_action_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_type: Mapped[str | None] = mapped_column(String(120))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
