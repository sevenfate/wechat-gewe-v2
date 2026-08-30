from __future__ import annotations

import json

import httpx
import pytest
import respx

from wechat_bot.gewe.client import (
    GeWeAPIError,
    GeWeClient,
    GeWeHTTPError,
    GeWeProtocolError,
    GeWeTransportError,
)
from wechat_bot.gewe.schemas import (
    AppIdRequest,
    ChatroomMemberListRequest,
    CheckLoginRequest,
    DeviceType,
    GetLoginQrCodeRequest,
    LoginStatus,
    PostTextRequest,
)

BASE_URL = "https://api.gewe.test"
TOKEN = "gewe-secret-token"


def _success(data: object | None = None) -> httpx.Response:
    payload: dict[str, object] = {"ret": 200, "msg": "操作成功"}
    if data is not None:
        payload["data"] = data
    return httpx.Response(200, json=payload)


@pytest.mark.asyncio
@respx.mock
async def test_login_and_account_endpoint_contracts() -> None:
    qr_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/getLoginQrCode").mock(
        return_value=_success(
            {
                "appId": "wx_app_1",
                "qrData": "http://weixin.qq.com/x/uuid-1",
                "qrImgBase64": "base64-data",
                "uuid": "uuid-1",
            }
        )
    )
    login_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/checkLogin").mock(
        return_value=_success(
            {
                "uuid": "uuid-1",
                "headImgUrl": "https://example.test/head.jpg",
                "nickName": "测试账号",
                "expiredTime": 230,
                "status": 2,
                "loginInfo": {
                    "uin": 4077276085,
                    "wxid": "wxid_user",
                    "nickName": "测试账号",
                    "mobile": None,
                    "alias": "wechat-alias",
                },
            }
        )
    )
    online_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/checkOnline").mock(
        return_value=_success(True)
    )
    reconnect_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/reconnection").mock(
        return_value=_success()
    )
    callback_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/setCallback").mock(
        return_value=_success()
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        qr_code = await client.get_login_qr_code(
            GetLoginQrCodeRequest(
                app_id="",
                device_type=DeviceType.MAC,
                region_id="320000",
                aid="123456",
            )
        )
        login = await client.check_login(
            CheckLoginRequest(
                app_id=qr_code.app_id,
                uuid=qr_code.uuid,
                auto_sliding=False,
                captcha_code="1234",
            )
        )
        online = await client.check_online(AppIdRequest(app_id=qr_code.app_id))
        reconnect_result = await client.reconnect(AppIdRequest(app_id=qr_code.app_id))
        await client.set_callback("https://bot.example.test/webhooks/gewe")

    assert qr_code.uuid == "uuid-1"
    assert login.status is LoginStatus.LOGGED_IN
    assert login.login_info is not None
    assert login.login_info.uin == "4077276085"
    assert online is True
    assert reconnect_result is None
    assert qr_route.calls.last.request.headers["X-GEWE-TOKEN"] == TOKEN
    assert json.loads(qr_route.calls.last.request.content) == {
        "appId": "",
        "type": "mac",
        "regionId": "320000",
        "aid": "123456",
    }
    assert json.loads(login_route.calls.last.request.content) == {
        "appId": "wx_app_1",
        "uuid": "uuid-1",
        "autoSliding": False,
        "captchCode": "1234",
    }
    assert json.loads(online_route.calls.last.request.content) == {"appId": "wx_app_1"}
    assert reconnect_route.called
    assert json.loads(callback_route.calls.last.request.content) == {
        "token": TOKEN,
        "callbackUrl": "https://bot.example.test/webhooks/gewe",
    }


@pytest.mark.asyncio
@respx.mock
async def test_directory_group_and_text_endpoint_contracts_keep_ids_as_strings() -> None:
    contacts_route = respx.post(f"{BASE_URL}/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=_success(
            {
                "friends": ["wxid_friend"],
                "chatrooms": ["123456789@chatroom"],
                "ghs": ["gh_official"],
            }
        )
    )
    members_route = respx.post(f"{BASE_URL}/gewe/v2/api/group/getChatroomMemberList").mock(
        return_value=_success(
            {
                "memberList": [
                    {
                        "wxid": "wxid_member",
                        "nickName": "群友",
                        "inviterUserName": None,
                        "memberFlag": 1,
                        "displayName": "群昵称",
                        "bigHeadImgUrl": "https://example.test/big.jpg",
                        "smallHeadImgUrl": "https://example.test/small.jpg",
                    }
                ],
                "chatroomOwner": "wxid_owner",
                "adminWxid": ["wxid_admin"],
            }
        )
    )
    text_route = respx.post(f"{BASE_URL}/gewe/v2/api/message/postText").mock(
        return_value=_success(
            {
                "toWxid": "123456789@chatroom",
                "createTime": 1703841160,
                "msgId": 0,
                "newMsgId": 3768973957878705000,
                "type": 1,
            }
        )
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        contacts = await client.fetch_contacts(AppIdRequest(app_id="wx_app_1"))
        members = await client.get_chatroom_member_list(
            ChatroomMemberListRequest(app_id="wx_app_1", chatroom_id="123456789@chatroom")
        )
        sent = await client.post_text(
            PostTextRequest(
                app_id="wx_app_1",
                to_wxid="123456789@chatroom",
                content="@群友 你好",
                ats="wxid_member",
            )
        )

    assert contacts.official_accounts == ["gh_official"]
    assert members.owner_wxid == "wxid_owner"
    assert members.members[0].display_name == "群昵称"
    assert sent.msg_id == "0"
    assert sent.new_msg_id == "3768973957878705000"
    assert json.loads(contacts_route.calls.last.request.content) == {"appId": "wx_app_1"}
    assert json.loads(members_route.calls.last.request.content) == {
        "appId": "wx_app_1",
        "chatroomId": "123456789@chatroom",
    }
    assert json.loads(text_route.calls.last.request.content) == {
        "appId": "wx_app_1",
        "toWxid": "123456789@chatroom",
        "content": "@群友 你好",
        "ats": "wxid_member",
    }


@pytest.mark.asyncio
@respx.mock
async def test_http_200_business_error_is_typed_retryable_and_redacted() -> None:
    respx.post(f"{BASE_URL}/gewe/v2/api/login/setCallback").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 500,
                "msg": f"upstream failed token={TOKEN}",
            },
        )
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        with pytest.raises(GeWeAPIError) as error_info:
            await client.set_callback("https://bot.example.test/webhooks/gewe")

    error = error_info.value
    assert error.ret == 500
    assert error.retryable is True
    assert TOKEN not in str(error)
    assert TOKEN not in error.provider_message


@pytest.mark.asyncio
@respx.mock
async def test_http_errors_are_classified_without_leaking_response_body() -> None:
    respx.post(f"{BASE_URL}/gewe/v2/api/login/checkOnline").mock(
        return_value=httpx.Response(401, text=f"invalid token {TOKEN}")
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        with pytest.raises(GeWeHTTPError) as error_info:
            await client.check_online(AppIdRequest(app_id="wx_app_1"))

    assert error_info.value.status_code == 401
    assert error_info.value.retryable is False
    assert TOKEN not in str(error_info.value)


@pytest.mark.asyncio
@respx.mock
async def test_timeout_is_retryable_and_protocol_error_is_not() -> None:
    timeout_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/checkOnline").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        with pytest.raises(GeWeTransportError) as timeout_info:
            await client.check_online(AppIdRequest(app_id="wx_app_1"))

    assert timeout_info.value.retryable is True

    timeout_route.mock(return_value=httpx.Response(200, content=b"not-json"))
    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        with pytest.raises(GeWeProtocolError) as protocol_info:
            await client.check_online(AppIdRequest(app_id="wx_app_1"))

    assert protocol_info.value.retryable is False


@pytest.mark.asyncio
@respx.mock
async def test_network_disconnect_is_retryable() -> None:
    respx.post(f"{BASE_URL}/gewe/v2/api/login/checkOnline").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        with pytest.raises(GeWeTransportError) as error_info:
            await client.check_online(AppIdRequest(app_id="wx_app_1"))

    assert error_info.value.retryable is True
    assert TOKEN not in str(error_info.value)


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="token cannot be empty"):
        GeWeClient(base_url=BASE_URL, token="")
