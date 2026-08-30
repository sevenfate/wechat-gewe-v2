from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from wechat_bot.db.models import BotAccountStatus
from wechat_bot.gewe.schemas import DeviceType, LoginStatus


class BotAccountView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    gewe_connection_id: UUID
    app_id: str
    wxid: str | None
    alias: str | None
    nickname: str | None
    avatar_url: str | None
    note: str | None
    qr_expires_at: datetime | None
    last_status_checked_at: datetime | None
    last_status_error: str | None
    status: BotAccountStatus
    logged_in_at: datetime | None
    last_online_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BotAccountList(BaseModel):
    items: list[BotAccountView]
    total: int


class ManualBotAccountCreate(BaseModel):
    app_id: Annotated[str, Field(min_length=1, max_length=255)]
    wxid: Annotated[str | None, Field(max_length=255)] = None
    note: Annotated[str | None, Field(max_length=500)] = None


class LoginQrCodeRequest(BaseModel):
    device_type: DeviceType
    region_id: Annotated[str, Field(min_length=1, max_length=40)]
    app_id: Annotated[str, Field(max_length=255)] = ""
    proxy_ip: Annotated[str | None, Field(max_length=1000)] = None
    ttuid: Annotated[str | None, Field(max_length=255)] = None
    aid: Annotated[str | None, Field(max_length=255)] = None


class LoginQrCodeResult(BaseModel):
    account: BotAccountView
    qr_data: str
    qr_image_base64: str
    uuid: str
    expires_at: datetime


class LoginCheckRequest(BaseModel):
    auto_sliding: bool | None = None
    proxy_ip: Annotated[str | None, Field(max_length=1000)] = None
    captcha_code: Annotated[str | None, Field(max_length=100)] = None


class LoginCheckResult(BaseModel):
    account: BotAccountView
    login_status: LoginStatus | None
    verification_url: str | None


class OnlineCheckResult(BaseModel):
    account: BotAccountView
    online: bool


class ReconnectResult(BaseModel):
    account: BotAccountView
    login_status: LoginStatus | None


class BotAccountStatusUpdate(BaseModel):
    disabled: bool
