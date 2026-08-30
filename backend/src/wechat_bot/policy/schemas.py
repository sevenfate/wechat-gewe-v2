from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    PrincipalType,
)


class PrincipalCreate(BaseModel):
    workspace_id: UUID
    principal_type: PrincipalType
    external_id: Annotated[str, Field(min_length=1, max_length=255)]
    display_name: Annotated[str | None, Field(max_length=255)] = None


class PrincipalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    principal_type: PrincipalType
    external_id: str
    display_name: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class GroupMemberPrincipalLookup(BaseModel):
    workspace_id: UUID
    chatroom_id: UUID
    membership_id: UUID
    principal: PrincipalView | None


class GroupMemberPrincipalEnsure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    chatroom_id: UUID
    membership_id: UUID


class AclRuleCreate(BaseModel):
    workspace_id: UUID
    principal_id: UUID | None = None
    scope_type: AclScopeType
    scope_id: Annotated[str, Field(min_length=1, max_length=255)]
    resource_type: AclResourceType
    resource_id: Annotated[str, Field(min_length=1, max_length=255)]
    effect: AclEffect
    locked: bool = False
    membership_epoch: int | None = Field(default=None, ge=1)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def validate_rule(self) -> AclRuleCreate:
        if self.locked and self.effect is not AclEffect.DENY:
            raise ValueError("only DENY rules can be locked")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        if self.resource_type is AclResourceType.CATEGORY and self.resource_id != "*":
            raise ValueError("category rules must use '*' as resource_id")
        return self


class AclRuleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    principal_id: UUID | None
    scope_type: AclScopeType
    scope_id: str
    resource_type: AclResourceType
    resource_id: str
    effect: AclEffect
    locked: bool
    membership_epoch: int | None
    valid_from: datetime
    valid_until: datetime | None
    reason: str
    created_by: str
    revoked_at: datetime | None
    revoked_by: str | None
    created_at: datetime
    updated_at: datetime


class AclEvaluationRequest(BaseModel):
    workspace_id: UUID
    bot_account_id: UUID
    actor_principal_id: UUID | None = None
    chatroom_id: UUID | None = None
    contact_id: UUID | None = None
    resource_type: AclResourceType
    resource_id: Annotated[str, Field(min_length=1, max_length=255)]
    parent_plugin_id: Annotated[str | None, Field(max_length=255)] = None
    trace_id: UUID | None = None


class AclDecisionView(BaseModel):
    effect: AclEffect
    allowed: bool
    reason: str
    policy_version: int
    matched_rule_ids: list[UUID]


class AclRuleList(BaseModel):
    items: list[AclRuleView]
    total: int
