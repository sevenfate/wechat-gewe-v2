from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContactView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_account_id: UUID
    external_id: str
    contact_type: str
    nickname: str | None
    remark: str | None
    avatar_url: str | None
    active: bool
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ContactList(BaseModel):
    items: list[ContactView]
    total: int


class ChatroomView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bot_account_id: UUID
    chatroom_id: str
    name: str | None
    owner_wxid: str | None
    member_count: int | None
    discovered_from: str
    placeholder: bool
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChatroomList(BaseModel):
    items: list[ChatroomView]
    total: int


class MembershipView(BaseModel):
    id: UUID
    chatroom_id: UUID
    member_wxid: str
    membership_epoch: int
    nickname: str | None
    display_name: str | None
    inviter_wxid: str | None
    member_flag: int | None
    joined_at: datetime
    left_at: datetime | None
    active: bool
    created_at: datetime
    updated_at: datetime


class MembershipList(BaseModel):
    items: list[MembershipView]
    total: int


class MembershipDepartureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    membership_epoch: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason cannot be blank")
        return normalized


class DirectorySyncResult(BaseModel):
    bot_account_id: UUID
    observed_contacts: int
    observed_chatrooms: int
    synced_at: datetime


class MembershipSyncResult(BaseModel):
    chatroom_id: UUID
    observed_members: int
    retained_unseen_active_members: int
    snapshot_complete: bool
    synced_at: datetime
