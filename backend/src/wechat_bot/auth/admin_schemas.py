from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
)

from wechat_bot.db.auth_models import AdminUserStatus

PermissionCode = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$",
    ),
]
RoleCode = Annotated[
    str,
    StringConstraints(min_length=2, max_length=120, pattern=r"^[a-z][a-z0-9_-]*$"),
]


class AdminUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    password: SecretStr

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("password")
    @classmethod
    def validate_password_size(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        if len(password.encode("utf-8")) > 1_024:
            raise ValueError("password is too long")
        return value


class AdminUserStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AdminUserStatus


class UserRoleBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_codes: list[RoleCode] = Field(default_factory=list, max_length=100)

    @field_validator("role_codes")
    @classmethod
    def reject_duplicate_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("role codes must be unique")
        return value


class AdminUserView(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    status: AdminUserStatus
    auth_version: int
    roles: list[str]
    created_at: datetime
    updated_at: datetime


class AdminUserList(BaseModel):
    items: list[AdminUserView]
    total: int


class RbacRoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RoleCode
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("role name cannot be blank")
        return normalized


class RolePermissionBindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_codes: list[PermissionCode] = Field(default_factory=list, max_length=500)

    @field_validator("permission_codes")
    @classmethod
    def reject_duplicate_permissions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("permission codes must be unique")
        return value


class RbacRoleView(BaseModel):
    id: UUID
    code: str
    name: str
    is_system: bool
    active: bool
    permissions: list[str]
    created_at: datetime
    updated_at: datetime


class RbacRoleList(BaseModel):
    items: list[RbacRoleView]
    total: int


class RbacPermissionView(BaseModel):
    id: UUID
    code: str
    description: str | None


class RbacPermissionList(BaseModel):
    items: list[RbacPermissionView]
    total: int
