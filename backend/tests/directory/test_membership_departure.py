from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.auth.constants import CSRF_HEADER_NAME
from wechat_bot.db.auth_models import AdminUser
from wechat_bot.db.models import (
    AuditEvent,
    Chatroom,
    ChatroomMembership,
    GeweConnection,
)

from .conftest import DirectorySeed

OPERATOR_PASSWORD = "directory operator password 123"


@dataclass(frozen=True, slots=True)
class DepartureSeed:
    workspace_id: UUID
    chatroom_id: UUID
    membership_id: UUID


async def _seed_membership(app: FastAPI, directory_seed: DirectorySeed) -> DepartureSeed:
    async with app.state.database.session_factory() as session:
        connection = await session.get(GeweConnection, directory_seed.connection_id)
        assert connection is not None
        chatroom = Chatroom(
            bot_account_id=directory_seed.bot_account_id,
            chatroom_id="departure-test@chatroom",
            name="Departure test",
            discovered_from="TEST",
            placeholder=False,
        )
        session.add(chatroom)
        await session.flush()
        membership = ChatroomMembership(
            chatroom_id=chatroom.id,
            member_wxid="wxid_departure_member",
            membership_epoch=1,
            nickname="Departure member",
        )
        session.add(membership)
        await session.commit()
        return DepartureSeed(
            workspace_id=connection.workspace_id,
            chatroom_id=chatroom.id,
            membership_id=membership.id,
        )


async def _create_and_login_operator(
    client: AsyncClient,
    *,
    suffix: str,
    permission_codes: list[str],
) -> None:
    role_code = f"directory-{suffix}"
    username = f"directory-{suffix}-operator"
    role = await client.post(
        "/api/v1/admin/roles",
        json={"code": role_code, "name": f"Directory {suffix}"},
    )
    assert role.status_code == 201
    permissions = await client.put(
        f"/api/v1/admin/roles/{role.json()['id']}/permissions",
        json={"permission_codes": permission_codes},
    )
    assert permissions.status_code == 200
    user = await client.post(
        "/api/v1/admin/users",
        json={
            "username": username,
            "display_name": f"Directory {suffix} operator",
            "password": OPERATOR_PASSWORD,
        },
    )
    assert user.status_code == 201
    binding = await client.put(
        f"/api/v1/admin/users/{user.json()['id']}/roles",
        json={"role_codes": [role_code]},
    )
    assert binding.status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 200

    csrf = await client.get("/api/auth/csrf")
    login = await client.post(
        "/api/auth/login",
        headers={CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
        json={"username": username, "password": OPERATOR_PASSWORD},
    )
    assert login.status_code == 200
    client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]


async def test_mark_left_validates_epoch_hides_member_and_writes_audit(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
) -> None:
    seed = await _seed_membership(app, directory_seed)
    endpoint = (
        f"/api/v1/directory/chatrooms/{seed.chatroom_id}/memberships/{seed.membership_id}/mark-left"
    )

    missing_reason = await admin_client.post(endpoint, json={"membership_epoch": 1})
    blank_reason = await admin_client.post(
        endpoint,
        json={"membership_epoch": 1, "reason": "   "},
    )
    stale = await admin_client.post(
        endpoint,
        json={"membership_epoch": 2, "reason": "stale browser state"},
    )
    marked = await admin_client.post(
        endpoint,
        json={"membership_epoch": 1, "reason": "  群管理员已人工确认退群  "},
    )
    repeated = await admin_client.post(
        endpoint,
        json={"membership_epoch": 1, "reason": "duplicate confirmation"},
    )
    active_members = await admin_client.get(
        f"/api/v1/directory/chatrooms/{seed.chatroom_id}/members"
    )
    all_members = await admin_client.get(
        f"/api/v1/directory/chatrooms/{seed.chatroom_id}/members",
        params={"include_left": True},
    )

    assert missing_reason.status_code == 422
    assert blank_reason.status_code == 422
    assert stale.status_code == 409
    assert marked.status_code == 200
    assert marked.json()["id"] == str(seed.membership_id)
    assert marked.json()["membership_epoch"] == 1
    assert marked.json()["active"] is False
    assert marked.json()["left_at"] is not None
    assert repeated.status_code == 409
    assert active_members.status_code == 200
    assert active_members.json()["total"] == 0
    assert all_members.status_code == 200
    assert all_members.json()["total"] == 1
    assert all_members.json()["items"][0]["active"] is False

    async with app.state.database.session_factory() as session:
        audits = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "directory.membership.mark_left")
            )
        )
        owner = await session.scalar(select(AdminUser).where(AdminUser.username == "test-owner"))

    assert len(audits) == 1
    audit = audits[0]
    assert owner is not None
    assert audit.workspace_id == seed.workspace_id
    assert audit.actor_type == "ADMIN_USER"
    assert audit.actor_id == str(owner.id)
    assert audit.object_type == "chatroom_membership"
    assert audit.object_id == str(seed.membership_id)
    assert audit.result == "SUCCESS"
    assert audit.detail == {
        "chatroom_id": str(seed.chatroom_id),
        "member_wxid": "wxid_departure_member",
        "membership_epoch": 1,
        "operator_username": "test-owner",
        "reason": "群管理员已人工确认退群",
        "closed_active_memberships": 1,
    }


@pytest.mark.parametrize(
    ("suffix", "permission_codes"),
    [
        ("without-policy", ["directory.read", "directory.sync"]),
        ("without-sync", ["directory.read", "policy.write"]),
    ],
)
async def test_mark_left_requires_sync_and_policy_permissions(
    app: FastAPI,
    admin_client: AsyncClient,
    directory_seed: DirectorySeed,
    suffix: str,
    permission_codes: list[str],
) -> None:
    seed = await _seed_membership(app, directory_seed)
    await _create_and_login_operator(
        admin_client,
        suffix=suffix,
        permission_codes=permission_codes,
    )

    response = await admin_client.post(
        (
            f"/api/v1/directory/chatrooms/{seed.chatroom_id}"
            f"/memberships/{seed.membership_id}/mark-left"
        ),
        json={"membership_epoch": 1, "reason": "must be forbidden"},
    )

    assert response.status_code == 403
    async with app.state.database.session_factory() as session:
        membership = await session.get(ChatroomMembership, seed.membership_id)
        audit = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "directory.membership.mark_left")
        )
    assert membership is not None
    assert membership.left_at is None
    assert audit is None
