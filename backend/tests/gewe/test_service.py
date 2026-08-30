from __future__ import annotations

import json

import httpx
import pytest
import respx

from wechat_bot.gewe.client import GeWeClient
from wechat_bot.gewe.schemas import DeviceType, LoginStatus
from wechat_bot.gewe.service import GeWeService

BASE_URL = "https://api.gewe.test"
TOKEN = "gewe-secret-token"


@pytest.mark.asyncio
@respx.mock
async def test_send_text_serializes_unique_mentions_in_order() -> None:
    route = respx.post(f"{BASE_URL}/gewe/v2/api/message/postText").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "toWxid": "123456789@chatroom",
                    "createTime": 1703841160,
                    "msgId": 0,
                    "newMsgId": 3768973957878705000,
                    "type": 1,
                },
            },
        )
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        service = GeWeService(client)
        result = await service.send_text(
            app_id="wx_app_1",
            to_wxid="123456789@chatroom",
            content="@甲 @乙 你好",
            at_wxids=["wxid_a", "wxid_b", "wxid_a"],
        )

    assert result.new_msg_id == "3768973957878705000"
    assert json.loads(route.calls.last.request.content)["ats"] == "wxid_a,wxid_b"


@pytest.mark.asyncio
@respx.mock
async def test_set_callback_is_only_called_explicitly() -> None:
    callback_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/setCallback").mock(
        return_value=httpx.Response(200, json={"ret": 200, "msg": "操作成功"})
    )
    online_route = respx.post(f"{BASE_URL}/gewe/v2/api/login/checkOnline").mock(
        return_value=httpx.Response(200, json={"ret": 200, "msg": "操作成功", "data": True})
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        service = GeWeService(client)
        assert await service.check_online(app_id="wx_app_1") is True
        assert callback_route.called is False
        await service.set_callback(callback_url="https://bot.example.test/webhooks/gewe")

    assert callback_route.call_count == 1
    assert online_route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_service_exposes_login_directory_and_group_operations() -> None:
    respx.post(f"{BASE_URL}/gewe/v2/api/login/getLoginQrCode").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "appId": "wx_app_1",
                    "qrData": "http://weixin.qq.com/x/uuid-1",
                    "qrImgBase64": "base64-data",
                    "uuid": "uuid-1",
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/gewe/v2/api/login/checkLogin").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "uuid": "uuid-1",
                    "expiredTime": 145,
                    "status": 0,
                    "loginInfo": None,
                },
            },
        )
    )
    respx.post(f"{BASE_URL}/gewe/v2/api/login/reconnection").mock(
        return_value=httpx.Response(200, json={"ret": 200, "msg": "操作成功"})
    )
    respx.post(f"{BASE_URL}/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {"friends": [], "chatrooms": [], "ghs": []},
            },
        )
    )
    respx.post(f"{BASE_URL}/gewe/v2/api/group/getChatroomMemberList").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "memberList": [],
                    "chatroomOwner": None,
                    "adminWxid": None,
                },
            },
        )
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        service = GeWeService(client)
        qr_code = await service.get_login_qr_code(device_type=DeviceType.MAC, region_id="320000")
        login = await service.check_login(app_id=qr_code.app_id, uuid=qr_code.uuid)
        reconnect = await service.reconnect(app_id=qr_code.app_id)
        contacts = await service.fetch_contacts(app_id=qr_code.app_id)
        members = await service.get_chatroom_member_list(
            app_id=qr_code.app_id, chatroom_id="123456789@chatroom"
        )

    assert login.status is LoginStatus.NOT_SCANNED
    assert reconnect is None
    assert contacts.friends == []
    assert members.owner_wxid is None


@pytest.mark.asyncio
@respx.mock
async def test_send_text_omits_ats_without_mentions() -> None:
    route = respx.post(f"{BASE_URL}/gewe/v2/api/message/postText").mock(
        return_value=httpx.Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "toWxid": "wxid_friend",
                    "createTime": 1703841160,
                    "msgId": 0,
                    "newMsgId": 3768973957878705000,
                    "type": 1,
                },
            },
        )
    )

    async with GeWeClient(base_url=BASE_URL, token=TOKEN) as client:
        service = GeWeService(client)
        await service.send_text(app_id="wx_app_1", to_wxid="wxid_friend", content="你好")

    assert "ats" not in json.loads(route.calls.last.request.content)
