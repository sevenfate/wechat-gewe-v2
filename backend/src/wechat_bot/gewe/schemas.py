from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def _to_external_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("identifier must be a string or integer")
    return str(value)


ExternalId = Annotated[str, BeforeValidator(_to_external_id)]


class GeWeModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class DeviceType(StrEnum):
    IPAD = "ipad"
    MAC = "mac"


class LoginStatus(IntEnum):
    NOT_SCANNED = 0
    SCANNED = 1
    LOGGED_IN = 2


class GetLoginQrCodeRequest(GeWeModel):
    app_id: ExternalId = Field(default="", validation_alias="appId", serialization_alias="appId")
    device_type: DeviceType = Field(validation_alias="type", serialization_alias="type")
    region_id: ExternalId = Field(validation_alias="regionId", serialization_alias="regionId")
    proxy_ip: str | None = Field(
        default=None, validation_alias="proxyIp", serialization_alias="proxyIp"
    )
    ttuid: ExternalId | None = None
    aid: ExternalId | None = None


class CheckLoginRequest(GeWeModel):
    app_id: ExternalId = Field(validation_alias="appId", serialization_alias="appId")
    uuid: ExternalId
    auto_sliding: bool | None = Field(
        default=None, validation_alias="autoSliding", serialization_alias="autoSliding"
    )
    proxy_ip: str | None = Field(
        default=None, validation_alias="proxyIp", serialization_alias="proxyIp"
    )
    captcha_code: str | None = Field(
        default=None, validation_alias="captchCode", serialization_alias="captchCode"
    )


class AppIdRequest(GeWeModel):
    app_id: ExternalId = Field(validation_alias="appId", serialization_alias="appId")


class SetCallbackRequest(GeWeModel):
    token: str
    callback_url: str = Field(validation_alias="callbackUrl", serialization_alias="callbackUrl")

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("callback URL must use HTTP or HTTPS")
        return value


class ChatroomMemberListRequest(AppIdRequest):
    chatroom_id: ExternalId = Field(validation_alias="chatroomId", serialization_alias="chatroomId")


class PostTextRequest(AppIdRequest):
    to_wxid: ExternalId = Field(validation_alias="toWxid", serialization_alias="toWxid")
    content: str
    ats: str | None = None


class LoginQrCodeData(GeWeModel):
    app_id: ExternalId = Field(alias="appId")
    qr_data: str = Field(alias="qrData")
    qr_image_base64: str = Field(alias="qrImgBase64")
    uuid: ExternalId


class LoginInfo(GeWeModel):
    uin: ExternalId
    wxid: ExternalId
    nickname: str = Field(alias="nickName")
    mobile: str | None = None
    alias: str | None = None


class LoginStatusData(GeWeModel):
    uuid: ExternalId | None = None
    head_image_url: str | None = Field(default=None, alias="headImgUrl")
    nickname: str | None = Field(default=None, alias="nickName")
    expired_time: int | None = Field(default=None, alias="expiredTime")
    status: LoginStatus | None = None
    login_info: LoginInfo | None = Field(default=None, alias="loginInfo")
    verification_url: str | None = Field(default=None, alias="url")


class ContactsData(GeWeModel):
    friends: list[ExternalId]
    chatrooms: list[ExternalId]
    official_accounts: list[ExternalId] = Field(alias="ghs")


class ChatroomMember(GeWeModel):
    wxid: ExternalId
    nickname: str = Field(alias="nickName")
    inviter_wxid: ExternalId | None = Field(default=None, alias="inviterUserName")
    member_flag: int = Field(alias="memberFlag")
    display_name: str | None = Field(default=None, alias="displayName")
    big_head_image_url: str = Field(alias="bigHeadImgUrl")
    small_head_image_url: str = Field(alias="smallHeadImgUrl")


class ChatroomMemberListData(GeWeModel):
    members: list[ChatroomMember] = Field(alias="memberList")
    owner_wxid: ExternalId | None = Field(default=None, alias="chatroomOwner")
    admin_wxids: list[ExternalId] | None = Field(default=None, alias="adminWxid")


class SentTextData(GeWeModel):
    to_wxid: ExternalId = Field(alias="toWxid")
    create_time: int = Field(alias="createTime")
    msg_id: ExternalId = Field(alias="msgId")
    new_msg_id: ExternalId = Field(alias="newMsgId")
    message_type: int = Field(alias="type")


class GeWeResponseBase(GeWeModel):
    ret: int
    msg: str


class GeWeResponse[ResponseDataT](GeWeResponseBase):
    data: ResponseDataT | None = None


class OperationResponse(GeWeResponse[dict[str, object]]):
    pass


class LoginQrCodeResponse(GeWeResponse[LoginQrCodeData]):
    pass


class CheckLoginResponse(GeWeResponse[LoginStatusData]):
    pass


class CheckOnlineResponse(GeWeResponse[bool]):
    pass


class ReconnectionResponse(GeWeResponse[LoginStatusData]):
    pass


class ContactsResponse(GeWeResponse[ContactsData]):
    pass


class ChatroomMemberListResponse(GeWeResponse[ChatroomMemberListData]):
    pass


class PostTextResponse(GeWeResponse[SentTextData]):
    pass
