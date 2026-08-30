from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from wechat_bot.db.plugin_models import (
    PluginActivationStatus,
    PluginDeploymentStatus,
    PluginPackageStatus,
)


class BuiltinPluginInstall(BaseModel):
    workspace_id: UUID


class PluginView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    plugin_id: str
    name: str
    description: str
    retired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PluginPackageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    plugin_id: UUID
    semantic_version: str
    package_sha256: str
    manifest: dict[str, Any]
    status: PluginPackageStatus
    created_at: datetime
    updated_at: datetime


class PluginInstallResult(BaseModel):
    plugin: PluginView
    package: PluginPackageView


class PluginDeploymentCreate(BaseModel):
    workspace_id: UUID
    plugin_id: UUID
    package_version_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=120)]
    config: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    grants: list[str] = Field(default_factory=list)


class PluginRevisionCreate(BaseModel):
    source_revision_id: UUID | None = None
    package_version_id: UUID | None = None
    config: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    grants: list[str] | None = None


class PluginRevisionDraft(BaseModel):
    source_revision_id: UUID
    package_version_id: UUID
    config: dict[str, Any]
    scope: dict[str, Any]
    grants: list[str]
    secret_placeholder: str


class PluginDeploymentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    plugin_id: UUID
    name: str
    status: PluginDeploymentStatus
    active_revision_id: UUID | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class PluginRevisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    package_version_id: UUID
    revision_number: int
    config_fingerprint: str
    scope: dict[str, Any]
    grants: list[str]
    content_sha256: str
    created_at: datetime
    updated_at: datetime


class PluginDeploymentResult(BaseModel):
    deployment: PluginDeploymentView
    revision: PluginRevisionView


class PluginActivationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    deployment_id: UUID
    revision_id: UUID
    activation_epoch: int
    status: PluginActivationStatus
    started_at: datetime | None
    stopped_at: datetime | None
    error_detail: str | None
    created_at: datetime
    updated_at: datetime


class PluginActivationResult(BaseModel):
    deployment: PluginDeploymentView
    activation: PluginActivationView


class PluginInvocation(BaseModel):
    method: Literal["health", "handle_event", "invoke_tool"]
    params: dict[str, Any] = Field(default_factory=dict)


class PluginInvocationResult(BaseModel):
    activation_epoch: int
    result: Any


class PluginCatalogView(BaseModel):
    plugins: list[PluginView]
    packages: list[PluginPackageView]
    deployments: list[PluginDeploymentView]
    revisions: list[PluginRevisionView]


class PluginContextView(BaseModel):
    workspace_id: UUID
    name: str
