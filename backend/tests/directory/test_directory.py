from __future__ import annotations

import json
from uuid import UUID

import respx
from fastapi import FastAPI
from httpx import AsyncClient, Request, Response
from sqlalchemy import select

from wechat_bot.db.models import Chatroom, ChatroomMembership, Contact

from .conftest import DirectorySeed


def _contacts_response() -> Response:
    return Response(
        200,
        json={
            "ret": 200,
            "msg": "操作成功",
            "data": {
                "friends": [
                    "wxid_friend",
                    9007199254740993,
                    "wxid_friend",
                ],
                "chatrooms": [
                    "12345678901234567890@chatroom",
                    "12345678901234567890@chatroom",
                ],
                "ghs": ["gh_official"],
            },
        },
    )


def _brief_info_response(
    *,
    group_name: str = "123",
    friend_name: str = "好友甲",
    friend_remark: str = "甲备注",
    friend_avatar: str = "https://example.test/friend-small.jpg",
) -> Response:
    return Response(
        200,
        json={
            "ret": 200,
            "msg": "操作成功",
            "data": [
                {
                    "userName": "wxid_friend",
                    "nickName": friend_name,
                    "remark": friend_remark,
                    "bigHeadImgUrl": "https://example.test/friend-big.jpg",
                    "smallHeadImgUrl": friend_avatar,
                },
                {
                    "userName": 9007199254740993,
                    "nickName": "好友乙",
                    "remark": "",
                    "bigHeadImgUrl": "https://example.test/friend-two-big.jpg",
                    "smallHeadImgUrl": "https://example.test/friend-two-small.jpg",
                },
                {
                    "userName": "12345678901234567890@chatroom",
                    "nickName": group_name,
                    "remark": "",
                    "bigHeadImgUrl": "https://example.test/group-big.jpg",
                    "smallHeadImgUrl": "https://example.test/group-small.jpg",
                },
            ],
        },
    )


async def _sync_contacts(admin_client: AsyncClient, seed: DirectorySeed) -> dict[str, object]:
    response = await admin_client.post(f"/api/v1/directory/bot-accounts/{seed.bot_account_id}/sync")
    assert response.status_code == 200
    return response.json()


@respx.mock
async def test_contact_sync_is_idempotent_and_creates_discovered_group_placeholder(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
) -> None:
    route = respx.post("https://api.gewe.test/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=_contacts_response()
    )
    brief_route = respx.post("https://api.gewe.test/gewe/v2/api/contacts/getBriefInfo").mock(
        return_value=_brief_info_response(group_name="Old group name")
    )
    detail_route = respx.post("https://api.gewe.test/gewe/v2/api/group/getChatroomInfo").mock(
        return_value=Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "chatroomId": "12345678901234567890@chatroom",
                    "nickName": "123",
                    "chatRoomOwner": 9007199254740993,
                    "memberList": [{"wxid": "member-a"}, {"wxid": "member-b"}],
                },
            },
        )
    )

    first = await admin_client.post(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/sync"
    )
    brief_route.mock(
        return_value=_brief_info_response(
            group_name="123",
            friend_name="好友甲-新",
            friend_remark="新备注",
            friend_avatar="https://example.test/friend-new-small.jpg",
        )
    )
    second = await admin_client.post(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/sync"
    )
    contacts = await admin_client.get(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/contacts"
    )
    chatrooms = await admin_client.get(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/chatrooms"
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["observed_contacts"] == 3
    assert first.json()["observed_chatrooms"] == 1
    assert route.call_count == 2
    assert brief_route.call_count == 2
    assert detail_route.call_count == 2
    assert all(call.request.headers["X-GEWE-TOKEN"] == directory_seed.token for call in route.calls)
    assert all(
        json.loads(call.request.content)
        == {
            "appId": directory_seed.app_id,
            "wxids": [
                "wxid_friend",
                "9007199254740993",
                "12345678901234567890@chatroom",
            ],
        }
        for call in brief_route.calls
    )
    assert contacts.status_code == 200
    assert contacts.json()["total"] == 3
    assert {item["external_id"] for item in contacts.json()["items"]} == {
        "wxid_friend",
        "9007199254740993",
        "gh_official",
    }
    contacts_by_id = {item["external_id"]: item for item in contacts.json()["items"]}
    assert contacts_by_id["wxid_friend"]["nickname"] == "好友甲-新"
    assert contacts_by_id["wxid_friend"]["remark"] == "新备注"
    assert (
        contacts_by_id["wxid_friend"]["avatar_url"] == "https://example.test/friend-new-small.jpg"
    )
    assert contacts_by_id["9007199254740993"]["nickname"] == "好友乙"
    assert contacts_by_id["gh_official"]["nickname"] is None
    assert chatrooms.status_code == 200
    assert chatrooms.json()["total"] == 1
    chatroom = chatrooms.json()["items"][0]
    assert chatroom["chatroom_id"] == "12345678901234567890@chatroom"
    assert chatroom["name"] == "123"
    assert chatroom["placeholder"] is False
    assert chatroom["owner_wxid"] == "9007199254740993"
    assert chatroom["member_count"] == 2
    assert chatroom["discovered_from"] == "CONTACT_LIST"
    assert directory_seed.token not in first.text + second.text + contacts.text + chatrooms.text

    database = app.state.database
    async with database.session_factory() as session:
        stored_contacts = list(await session.scalars(select(Contact)))
        stored_chatrooms = list(await session.scalars(select(Chatroom)))
    assert len(stored_contacts) == 3
    assert len(stored_chatrooms) == 1


@respx.mock
async def test_member_sync_is_idempotent_and_never_marks_unseen_member_left(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
) -> None:
    respx.post("https://api.gewe.test/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=_contacts_response()
    )
    respx.post("https://api.gewe.test/gewe/v2/api/contacts/getBriefInfo").mock(
        return_value=_brief_info_response()
    )
    respx.post("https://api.gewe.test/gewe/v2/api/group/getChatroomInfo").mock(
        return_value=Response(
            200,
            json={"ret": 500, "msg": "detail unavailable"},
        )
    )
    sync_result = await _sync_contacts(admin_client, directory_seed)
    assert sync_result["chatroom_detail_status"] == "PARTIAL"
    assert sync_result["chatroom_detail_failures"] == ["12345678901234567890@chatroom"]
    chatrooms = await admin_client.get(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/chatrooms"
    )
    chatroom_uuid = chatrooms.json()["items"][0]["id"]

    members_route = respx.post(
        "https://api.gewe.test/gewe/v2/api/group/getChatroomMemberList"
    ).mock(
        return_value=Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "memberList": [
                        {
                            "wxid": 9007199254740993,
                            "nickName": "Member one",
                            "inviterUserName": None,
                            "memberFlag": 1,
                            "displayName": "One",
                            "bigHeadImgUrl": "https://example.test/one-big.jpg",
                            "smallHeadImgUrl": "https://example.test/one-small.jpg",
                        },
                        {
                            "wxid": "wxid_member_two",
                            "nickName": "Member two",
                            "inviterUserName": 9007199254740993,
                            "memberFlag": 2049,
                            "displayName": None,
                            "bigHeadImgUrl": "https://example.test/two-big.jpg",
                            "smallHeadImgUrl": "https://example.test/two-small.jpg",
                        },
                    ],
                    "chatroomOwner": 9007199254740993,
                    "adminWxid": None,
                },
            },
        )
    )
    first = await admin_client.post(f"/api/v1/directory/chatrooms/{chatroom_uuid}/sync-members")

    members_route.mock(
        return_value=Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "memberList": [
                        {
                            "wxid": 9007199254740993,
                            "nickName": "Member one renamed",
                            "inviterUserName": None,
                            "memberFlag": 1,
                            "displayName": "One renamed",
                            "bigHeadImgUrl": "https://example.test/one-big.jpg",
                            "smallHeadImgUrl": "https://example.test/one-small.jpg",
                        }
                    ],
                    "chatroomOwner": 9007199254740993,
                    "adminWxid": [],
                },
            },
        )
    )
    second = await admin_client.post(f"/api/v1/directory/chatrooms/{chatroom_uuid}/sync-members")
    listed = await admin_client.get(f"/api/v1/directory/chatrooms/{chatroom_uuid}/members")

    assert first.status_code == 200
    assert first.json()["observed_members"] == 2
    assert first.json()["snapshot_complete"] is False
    assert second.status_code == 200
    assert second.json()["observed_members"] == 1
    assert second.json()["retained_unseen_active_members"] == 1
    assert listed.status_code == 200
    assert listed.json()["total"] == 2
    listed_by_wxid = {item["member_wxid"]: item for item in listed.json()["items"]}
    assert set(listed_by_wxid) == {"9007199254740993", "wxid_member_two"}
    assert listed_by_wxid["9007199254740993"]["nickname"] == "Member one renamed"
    assert all(item["active"] is True for item in listed_by_wxid.values())
    assert all(item["membership_epoch"] == 1 for item in listed_by_wxid.values())

    database = app.state.database
    async with database.session_factory() as session:
        stored_chatroom = await session.get(Chatroom, UUID(chatroom_uuid))
        stored_memberships = list(await session.scalars(select(ChatroomMembership)))
    assert stored_chatroom is not None
    assert stored_chatroom.owner_wxid == "9007199254740993"
    assert stored_chatroom.member_count == 1
    assert stored_chatroom.placeholder is True
    assert len(stored_memberships) == 2
    assert all(membership.left_at is None for membership in stored_memberships)


@respx.mock
async def test_brief_info_batch_failure_keeps_existing_directory_atomic_and_safe(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
) -> None:
    existing_friend_id = "wxid_existing"
    existing_chatroom_id = "existing@chatroom"
    database = app.state.database
    async with database.session_factory() as session:
        session.add(
            Contact(
                bot_account_id=directory_seed.bot_account_id,
                external_id=existing_friend_id,
                contact_type="FRIEND",
                nickname="Existing friend",
                active=True,
            )
        )
        session.add(
            Chatroom(
                bot_account_id=directory_seed.bot_account_id,
                chatroom_id=existing_chatroom_id,
                name="Existing group",
                discovered_from="CONTACT_LIST",
                placeholder=True,
            )
        )
        await session.commit()

    friends = [existing_friend_id, *[f"wxid_new_{index}" for index in range(20)]]
    respx.post("https://api.gewe.test/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": {
                    "friends": friends,
                    "chatrooms": [existing_chatroom_id],
                    "ghs": ["gh_official"],
                },
            },
        )
    )
    batch_calls = 0

    def brief_response(request: Request) -> Response:
        nonlocal batch_calls
        batch_calls += 1
        wxids = json.loads(request.content)["wxids"]
        if batch_calls == 2:
            return Response(
                200,
                json={
                    "ret": 500,
                    "msg": f"brief failed token={directory_seed.token}",
                },
            )
        return Response(
            200,
            json={
                "ret": 200,
                "msg": "操作成功",
                "data": [{"userName": wxid, "nickName": f"Updated {wxid}"} for wxid in wxids],
            },
        )

    brief_route = respx.post("https://api.gewe.test/gewe/v2/api/contacts/getBriefInfo").mock(
        side_effect=brief_response
    )

    response = await admin_client.post(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/sync"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "GeWe directory sync failed"}
    assert directory_seed.token not in response.text
    assert [len(json.loads(call.request.content)["wxids"]) for call in brief_route.calls] == [20, 2]

    async with database.session_factory() as session:
        stored_contacts = list(await session.scalars(select(Contact)))
        stored_chatrooms = list(await session.scalars(select(Chatroom)))
    assert len(stored_contacts) == 1
    assert stored_contacts[0].external_id == existing_friend_id
    assert stored_contacts[0].nickname == "Existing friend"
    assert len(stored_chatrooms) == 1
    assert stored_chatrooms[0].chatroom_id == existing_chatroom_id
    assert stored_chatrooms[0].name == "Existing group"


@respx.mock
async def test_upstream_failure_is_safe_and_does_not_expose_token(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
) -> None:
    app.state.settings.directory_contacts_cache_poll_attempts = 1
    respx.post("https://api.gewe.test/gewe/v2/api/contacts/fetchContactsList").mock(
        return_value=Response(
            200,
            json={
                "ret": 500,
                "msg": f"upstream failed token={directory_seed.token}",
            },
        )
    )
    respx.post("https://api.gewe.test/gewe/v2/api/contacts/fetchContactsListCache").mock(
        return_value=Response(
            200,
            json={
                "ret": 500,
                "msg": f"cache failed token={directory_seed.token}",
            },
        )
    )

    response = await admin_client.post(
        f"/api/v1/directory/bot-accounts/{directory_seed.bot_account_id}/sync"
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "GeWe directory sync failed"}
    assert directory_seed.token not in response.text
    database = app.state.database
    async with database.session_factory() as session:
        stored_contacts = list(await session.scalars(select(Contact)))
        stored_chatrooms = list(await session.scalars(select(Chatroom)))
    assert stored_contacts == []
    assert stored_chatrooms == []


async def test_directory_read_and_sync_endpoints_return_not_found(
    admin_client: AsyncClient,
) -> None:
    missing_id = "00000000-0000-0000-0000-000000000001"

    contacts = await admin_client.get(f"/api/v1/directory/bot-accounts/{missing_id}/contacts")
    chatrooms = await admin_client.get(f"/api/v1/directory/bot-accounts/{missing_id}/chatrooms")
    members = await admin_client.get(f"/api/v1/directory/chatrooms/{missing_id}/members")
    sync = await admin_client.post(f"/api/v1/directory/bot-accounts/{missing_id}/sync")
    sync_members = await admin_client.post(f"/api/v1/directory/chatrooms/{missing_id}/sync-members")

    assert contacts.status_code == 404
    assert chatrooms.status_code == 404
    assert members.status_code == 404
    assert sync.status_code == 404
    assert sync_members.status_code == 404
