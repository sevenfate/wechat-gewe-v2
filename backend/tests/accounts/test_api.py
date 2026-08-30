from __future__ import annotations

import respx
from httpx import AsyncClient, Response


async def test_qr_login_persists_account_without_mobile_number(
    admin_client: AsyncClient,
) -> None:
    client = admin_client
    connection = await _connection(client)
    connection_id = connection["id"]

    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.gewe.test/gewe/v2/api/login/getLoginQrCode").mock(
            return_value=Response(
                200,
                json={
                    "ret": 200,
                    "msg": "操作成功",
                    "data": {
                        "appId": "app-qr-1",
                        "qrData": "http://weixin.qq.com/x/test",
                        "qrImgBase64": "data:image/png;base64,AAAA",
                        "uuid": "qr-uuid-1",
                    },
                },
            )
        )
        qr = await client.post(
            f"/api/v1/connections/{connection_id}/login/qr-code",
            json={"device_type": "mac", "region_id": "440000"},
        )

    assert qr.status_code == 200
    assert qr.json()["account"]["status"] == "QR_PENDING"
    account_id = qr.json()["account"]["id"]

    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.gewe.test/gewe/v2/api/login/checkLogin").mock(
            return_value=Response(
                200,
                json={
                    "ret": 200,
                    "msg": "操作成功",
                    "data": {
                        "uuid": "qr-uuid-1",
                        "headImgUrl": "https://avatar.example.test/a.jpg",
                        "nickName": "测试账号",
                        "expiredTime": 100,
                        "status": 2,
                        "loginInfo": {
                            "uin": 9_000_000_000_000_001,
                            "wxid": "wxid_test_account",
                            "nickName": "测试账号",
                            "mobile": "13800000000",
                            "alias": "test_alias",
                        },
                    },
                },
            )
        )
        checked = await client.post(
            f"/api/v1/bot-accounts/{account_id}/login/check",
            json={"auto_sliding": True},
        )

    assert checked.status_code == 200
    account = checked.json()["account"]
    assert account["status"] == "ONLINE"
    assert account["wxid"] == "wxid_test_account"
    assert account["alias"] == "test_alias"
    assert "mobile" not in checked.text


async def test_manual_registration_online_check_and_disabled_guard(
    admin_client: AsyncClient,
) -> None:
    client = admin_client
    connection = await _connection(client)
    registered = await client.post(
        f"/api/v1/connections/{connection['id']}/bot-accounts",
        json={"app_id": "app-manual-1", "wxid": "wxid_manual"},
    )
    account_id = registered.json()["id"]

    with respx.mock(assert_all_called=True) as router:
        router.post("https://api.gewe.test/gewe/v2/api/login/checkOnline").mock(
            return_value=Response(200, json={"ret": 200, "msg": "ok", "data": True})
        )
        online = await client.post(f"/api/v1/bot-accounts/{account_id}/check-online")

    assert online.status_code == 200
    assert online.json()["online"] is True
    assert online.json()["account"]["status"] == "ONLINE"

    disabled = await client.put(
        f"/api/v1/bot-accounts/{account_id}/disabled",
        json={"disabled": True},
    )
    blocked = await client.post(f"/api/v1/bot-accounts/{account_id}/check-online")
    assert disabled.json()["status"] == "DISABLED"
    assert blocked.status_code == 409


async def _connection(client: AsyncClient) -> dict[str, object]:
    response = await client.post(
        "/api/v1/connections",
        json={
            "name": "Primary",
            "api_base_url": "https://api.gewe.test",
            "token": "super-secret-token",
        },
    )
    assert response.status_code == 201
    return response.json()
