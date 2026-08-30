from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from wechat_bot.auth.constants import CSRF_HEADER_NAME
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ChatroomMembership,
    GeweConnection,
    Workspace,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    Principal,
    PrincipalType,
)
from wechat_bot.policy.schemas import AclRuleCreate, PrincipalCreate
from wechat_bot.policy.service import PolicyService

OPERATOR_PASSWORD = "policy audit operator password"
AUDITOR_PERMISSIONS = [
    "account.read",
    "connection.read",
    "directory.read",
    "plugin.read",
    "policy.read",
]


@dataclass(frozen=True, slots=True)
class PolicyApiSeed:
    workspace_id: UUID
    bot_account_id: UUID
    chatroom_id: UUID
    mapped_membership_id: UUID
    unmapped_membership_id: UUID
    mapped_principal_id: UUID


async def _seed_policy_data(app: FastAPI) -> PolicyApiSeed:
    async with app.state.database.session_factory() as session, session.begin():
        workspace = Workspace(name="Policy audit", slug="policy-audit")
        session.add(workspace)
        await session.flush()
        connection = GeweConnection(
            workspace_id=workspace.id,
            name="Policy audit connection",
            api_base_url="https://api.gewe.test",
            token_ciphertext=b"encrypted",
            token_fingerprint="0123456789abcdef",
            callback_secret_ciphertext=b"encrypted",
            callback_secret_hash="b" * 64,
        )
        session.add(connection)
        await session.flush()
        account = BotAccount(
            gewe_connection_id=connection.id,
            app_id="policy-audit-app",
            wxid="wxid_policy_bot",
            status=BotAccountStatus.ONLINE,
        )
        session.add(account)
        await session.flush()
        chatroom = Chatroom(
            bot_account_id=account.id,
            chatroom_id="policy-audit@chatroom",
            name="Policy audit group",
            discovered_from="TEST",
            placeholder=False,
        )
        session.add(chatroom)
        await session.flush()
        mapped_membership = ChatroomMembership(
            chatroom_id=chatroom.id,
            member_wxid="wxid_mapped_member",
            membership_epoch=1,
            display_name="Mapped member",
        )
        unmapped_membership = ChatroomMembership(
            chatroom_id=chatroom.id,
            member_wxid="wxid_unmapped_member",
            membership_epoch=1,
            display_name="Unmapped member",
        )
        session.add_all([mapped_membership, unmapped_membership])
        await session.flush()

        service = PolicyService()
        mapped_principal = await service.create_principal(
            session,
            PrincipalCreate(
                workspace_id=workspace.id,
                principal_type=PrincipalType.GROUP_MEMBER,
                external_id=mapped_membership.member_wxid,
                display_name=mapped_membership.display_name,
            ),
        )
        await service.create_rule(
            session,
            AclRuleCreate(
                workspace_id=workspace.id,
                principal_id=mapped_principal.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.PLUGIN,
                resource_id="builtin.echo",
                effect=AclEffect.DENY,
                reason="Auditor should see this member exception",
            ),
            created_by="test",
        )
        return PolicyApiSeed(
            workspace_id=workspace.id,
            bot_account_id=account.id,
            chatroom_id=chatroom.id,
            mapped_membership_id=mapped_membership.id,
            unmapped_membership_id=unmapped_membership.id,
            mapped_principal_id=mapped_principal.id,
        )


async def _create_operator(
    client: AsyncClient,
    *,
    suffix: str,
    permission_codes: list[str],
) -> str:
    role_code = f"policy-{suffix}"
    username = f"policy-{suffix}-operator"
    role = await client.post(
        "/api/v1/admin/roles",
        json={"code": role_code, "name": f"Policy {suffix}"},
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
            "display_name": f"Policy {suffix} operator",
            "password": OPERATOR_PASSWORD,
        },
    )
    assert user.status_code == 201
    binding = await client.put(
        f"/api/v1/admin/users/{user.json()['id']}/roles",
        json={"role_codes": [role_code]},
    )
    assert binding.status_code == 200
    return username


async def _login_operator(client: AsyncClient, username: str) -> None:
    assert (await client.post("/api/auth/logout")).status_code == 200
    csrf = await client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    login = await client.post(
        "/api/auth/login",
        headers={CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
        json={"username": username, "password": OPERATOR_PASSWORD},
    )
    assert login.status_code == 200
    client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]


async def _principal_count(app: FastAPI) -> int:
    async with app.state.database.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Principal))
        return count or 0


async def _principal_snapshot(
    app: FastAPI,
    principal_id: UUID,
) -> tuple[str | None, bool, datetime]:
    async with app.state.database.session_factory() as session:
        principal = await session.get(Principal, principal_id)
        assert principal is not None
        return principal.display_name, principal.active, principal.updated_at


def _lookup_params(seed: PolicyApiSeed, membership_id: UUID) -> dict[str, str]:
    return {
        "workspace_id": str(seed.workspace_id),
        "chatroom_id": str(seed.chatroom_id),
        "membership_id": str(membership_id),
    }


async def test_read_only_auditor_can_inspect_member_exceptions_without_creating_principal(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_policy_data(app)
    username = await _create_operator(
        admin_client,
        suffix="read-only",
        permission_codes=AUDITOR_PERMISSIONS,
    )
    await _login_operator(admin_client, username)
    principal_count_before = await _principal_count(app)
    mapped_principal_before = await _principal_snapshot(app, seed.mapped_principal_id)

    members = await admin_client.get(
        f"/api/v1/directory/chatrooms/{seed.chatroom_id}/members",
        params={"include_left": "false", "limit": "200", "offset": "0"},
    )
    mapped = await admin_client.get(
        "/api/v1/policy/principals/group-member",
        params=_lookup_params(seed, seed.mapped_membership_id),
    )
    rules = await admin_client.get(
        "/api/v1/policy/rules",
        params={
            "workspace_id": str(seed.workspace_id),
            "scope_type": "CHATROOM",
            "scope_id": str(seed.chatroom_id),
        },
    )
    unmapped = await admin_client.get(
        "/api/v1/policy/principals/group-member",
        params=_lookup_params(seed, seed.unmapped_membership_id),
    )

    assert members.status_code == 200
    assert {item["id"] for item in members.json()["items"]} == {
        str(seed.mapped_membership_id),
        str(seed.unmapped_membership_id),
    }
    assert mapped.status_code == 200
    assert mapped.json()["workspace_id"] == str(seed.workspace_id)
    assert mapped.json()["chatroom_id"] == str(seed.chatroom_id)
    assert mapped.json()["membership_id"] == str(seed.mapped_membership_id)
    assert mapped.json()["principal"]["id"] == str(seed.mapped_principal_id)
    assert mapped.json()["principal"]["workspace_id"] == str(seed.workspace_id)
    assert mapped.json()["principal"]["principal_type"] == "GROUP_MEMBER"
    assert mapped.json()["principal"]["external_id"] == "wxid_mapped_member"
    assert rules.status_code == 200
    assert [item["effect"] for item in rules.json()["items"]] == ["DENY"]
    assert rules.json()["items"][0]["principal_id"] == str(seed.mapped_principal_id)
    assert unmapped.status_code == 200
    assert unmapped.json()["principal"] is None
    assert await _principal_count(app) == principal_count_before
    assert await _principal_snapshot(app, seed.mapped_principal_id) == mapped_principal_before

    forbidden_principal = await admin_client.post(
        "/api/v1/policy/principals/group-member",
        json={
            "workspace_id": str(seed.workspace_id),
            "chatroom_id": str(seed.chatroom_id),
            "membership_id": str(seed.unmapped_membership_id),
        },
    )
    forbidden_rule = await admin_client.post(
        "/api/v1/policy/rules",
        json={
            "workspace_id": str(seed.workspace_id),
            "scope_type": "CHATROOM",
            "scope_id": str(seed.chatroom_id),
            "resource_type": "PLUGIN",
            "resource_id": "builtin.echo",
            "effect": "ALLOW",
            "reason": "read-only users cannot write",
        },
    )

    assert forbidden_principal.status_code == 403
    assert forbidden_rule.status_code == 403
    assert await _principal_count(app) == principal_count_before
    assert await _principal_snapshot(app, seed.mapped_principal_id) == mapped_principal_before


async def test_group_member_ensure_derives_identity_from_workspace_membership(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_policy_data(app)
    principal_count_before = await _principal_count(app)
    payload = {
        "workspace_id": str(seed.workspace_id),
        "chatroom_id": str(seed.chatroom_id),
        "membership_id": str(seed.unmapped_membership_id),
    }

    spoofed = await admin_client.post(
        "/api/v1/policy/principals/group-member",
        json={**payload, "external_id": "wxid_spoofed_member"},
    )
    created = await admin_client.post(
        "/api/v1/policy/principals/group-member",
        json=payload,
    )
    repeated = await admin_client.post(
        "/api/v1/policy/principals/group-member",
        json=payload,
    )

    assert spoofed.status_code == 422
    assert created.status_code == 201
    assert created.json()["workspace_id"] == str(seed.workspace_id)
    assert created.json()["principal_type"] == "GROUP_MEMBER"
    assert created.json()["external_id"] == "wxid_unmapped_member"
    assert created.json()["display_name"] == "Unmapped member"
    assert repeated.status_code == 201
    assert repeated.json()["id"] == created.json()["id"]
    assert await _principal_count(app) == principal_count_before + 1


async def test_group_member_lookup_requires_directory_read(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_policy_data(app)
    username = await _create_operator(
        admin_client,
        suffix="policy-only",
        permission_codes=["policy.read"],
    )
    await _login_operator(admin_client, username)

    response = await admin_client.get(
        "/api/v1/policy/principals/group-member",
        params=_lookup_params(seed, seed.mapped_membership_id),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "permission denied"


async def test_generic_principal_creation_is_not_exposed(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.post(
        "/api/v1/policy/principals",
        json={
            "workspace_id": str(uuid4()),
            "principal_type": "GROUP_MEMBER",
            "external_id": "wxid_spoofed_member",
            "display_name": "Spoofed member",
        },
    )

    assert response.status_code == 404


async def test_stale_membership_epoch_cannot_create_rule_for_current_membership(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_policy_data(app)

    response = await admin_client.post(
        "/api/v1/policy/rules",
        json={
            "workspace_id": str(seed.workspace_id),
            "principal_id": str(seed.mapped_principal_id),
            "scope_type": "CHATROOM",
            "scope_id": str(seed.chatroom_id),
            "resource_type": "PLUGIN",
            "resource_id": "builtin.weather",
            "effect": "ALLOW",
            "membership_epoch": 2,
            "reason": "stale browser must not authorize a new membership",
        },
    )
    rules = await admin_client.get(
        "/api/v1/policy/rules",
        params={
            "workspace_id": str(seed.workspace_id),
            "scope_type": "CHATROOM",
            "scope_id": str(seed.chatroom_id),
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "membership_epoch is stale"
    assert rules.status_code == 200
    assert all(item["resource_id"] != "builtin.weather" for item in rules.json()["items"])


async def test_group_member_lookup_rejects_workspace_or_chatroom_mismatch(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_policy_data(app)
    principal_count_before = await _principal_count(app)

    wrong_workspace = await admin_client.get(
        "/api/v1/policy/principals/group-member",
        params={
            **_lookup_params(seed, seed.mapped_membership_id),
            "workspace_id": str(uuid4()),
        },
    )
    wrong_chatroom = await admin_client.get(
        "/api/v1/policy/principals/group-member",
        params={
            **_lookup_params(seed, seed.mapped_membership_id),
            "chatroom_id": str(uuid4()),
        },
    )

    assert wrong_workspace.status_code == 404
    assert wrong_workspace.json()["detail"] == "active group membership not found in workspace"
    assert wrong_chatroom.status_code == 404
    assert wrong_chatroom.json()["detail"] == "active group membership not found in workspace"
    assert await _principal_count(app) == principal_count_before
