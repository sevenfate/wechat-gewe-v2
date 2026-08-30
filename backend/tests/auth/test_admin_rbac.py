from __future__ import annotations

import json
from uuid import UUID

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from wechat_bot.auth.constants import ADMIN_USER_MANAGE_PERMISSION
from wechat_bot.auth.passwords import password_manager
from wechat_bot.auth.service import ensure_system_permissions
from wechat_bot.db.auth_models import (
    AdminSession,
    AdminUser,
    AdminUserStatus,
    AuthSecurityEvent,
    RbacPermission,
    RbacRole,
    RbacUserRole,
)

from .helpers import login_client

MANAGED_USERNAME = "managed-admin"
MANAGED_PASSWORD = "managed administrator password"


async def _login_owner(client: AsyncClient) -> None:
    _, response = await login_client(client)
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


async def _create_user(
    client: AsyncClient,
    *,
    username: str = MANAGED_USERNAME,
    password: str = MANAGED_PASSWORD,
) -> dict[str, object]:
    response = await client.post(
        "/api/v1/admin/users",
        json={
            "username": username,
            "display_name": "Managed Administrator",
            "password": password,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_role(client: AsyncClient, code: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/admin/roles",
        json={"code": code, "name": code.replace("-", " ").title()},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_owner_manages_rbac_without_exposing_password_and_revokes_sessions(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _login_owner(client)

    permissions_response = await client.get("/api/v1/admin/permissions")
    assert permissions_response.status_code == 200
    permission_codes = {item["code"] for item in permissions_response.json()["items"]}
    assert {
        ADMIN_USER_MANAGE_PERMISSION,
        "directory.read",
        "directory.sync",
    } <= permission_codes

    created_user = await _create_user(client)
    serialized_user = json.dumps(created_user)
    assert "password" not in serialized_user
    assert "hash" not in serialized_user

    async with app.state.database.session_factory() as database:
        stored_user = await database.scalar(
            select(AdminUser).where(AdminUser.username_normalized == MANAGED_USERNAME)
        )
    assert stored_user is not None
    assert stored_user.password_hash.startswith("$argon2id$")
    assert MANAGED_PASSWORD not in stored_user.password_hash
    assert password_manager.verify(stored_user.password_hash, MANAGED_PASSWORD)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as managed_client:
        _, first_login = await login_client(
            managed_client,
            username=MANAGED_USERNAME,
            password=MANAGED_PASSWORD,
        )
        assert first_login.status_code == 200

        role = await _create_role(client, "directory-operator")
        initial_permissions = await client.put(
            f"/api/v1/admin/roles/{role['id']}/permissions",
            json={"permission_codes": ["directory.read"]},
        )
        assert initial_permissions.status_code == 200
        role_binding = await client.put(
            f"/api/v1/admin/users/{created_user['id']}/roles",
            json={"role_codes": ["directory-operator"]},
        )
        assert role_binding.status_code == 200
        assert role_binding.json()["roles"] == ["directory-operator"]
        assert (await managed_client.get("/api/auth/me")).status_code == 401

        _, second_login = await login_client(
            managed_client,
            username=MANAGED_USERNAME,
            password=MANAGED_PASSWORD,
        )
        assert second_login.status_code == 200
        assert second_login.json()["user"]["permissions"] == ["directory.read"]

        changed_permissions = await client.put(
            f"/api/v1/admin/roles/{role['id']}/permissions",
            json={"permission_codes": ["directory.read", "directory.sync"]},
        )
        assert changed_permissions.status_code == 200
        assert changed_permissions.json()["permissions"] == [
            "directory.read",
            "directory.sync",
        ]
        assert (await managed_client.get("/api/auth/me")).status_code == 401

    async with app.state.database.session_factory() as database:
        stored_user = await database.scalar(
            select(AdminUser).where(AdminUser.username_normalized == MANAGED_USERNAME)
        )
        sessions = list(
            await database.scalars(
                select(AdminSession).where(AdminSession.user_id == UUID(str(created_user["id"])))
            )
        )
        events = list(
            await database.scalars(
                select(AuthSecurityEvent).where(AuthSecurityEvent.event_type.like("auth.admin.%"))
            )
        )
    assert stored_user is not None
    assert stored_user.auth_version == 3
    assert {session.revoked_reason for session in sessions} == {
        "admin_user_roles_changed",
        "admin_role_permissions_changed",
    }
    assert {
        "auth.admin.user.created",
        "auth.admin.role.created",
        "auth.admin.user.roles_changed",
        "auth.admin.role.permissions_changed",
    } <= {event.event_type for event in events}
    audit_json = json.dumps([event.detail for event in events])
    assert MANAGED_PASSWORD not in audit_json
    assert "password_hash" not in audit_json


async def test_last_active_owner_cannot_be_disabled_or_unassigned(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _login_owner(client)
    users = (await client.get("/api/v1/admin/users")).json()["items"]
    owner = next(user for user in users if user["roles"] == ["owner"])

    disable = await client.patch(
        f"/api/v1/admin/users/{owner['id']}/status",
        json={"status": "DISABLED"},
    )
    assert disable.status_code == 409
    remove_role = await client.put(
        f"/api/v1/admin/users/{owner['id']}/roles",
        json={"role_codes": []},
    )
    assert remove_role.status_code == 409
    assert (await client.get("/api/auth/me")).status_code == 200

    second_owner = await _create_user(
        client,
        username="second-owner",
        password="second owner secure password",
    )
    assign_owner = await client.put(
        f"/api/v1/admin/users/{second_owner['id']}/roles",
        json={"role_codes": ["owner"]},
    )
    assert assign_owner.status_code == 200

    disable = await client.patch(
        f"/api/v1/admin/users/{owner['id']}/status",
        json={"status": "DISABLED"},
    )
    assert disable.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 401

    async with app.state.database.session_factory() as database:
        active_owner_count = await database.scalar(
            select(func.count(AdminUser.id))
            .select_from(AdminUser)
            .join(RbacUserRole, RbacUserRole.user_id == AdminUser.id)
            .join(RbacRole, RbacRole.id == RbacUserRole.role_id)
            .where(
                RbacRole.code == "owner",
                AdminUser.status == AdminUserStatus.ACTIVE,
            )
        )
    assert active_owner_count == 1


async def test_exact_management_permission_has_no_role_escalation_or_owner_control(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _login_owner(client)
    users = (await client.get("/api/v1/admin/users")).json()["items"]
    owner = next(user for user in users if user["roles"] == ["owner"])
    delegated = await _create_user(
        client,
        username="delegated-admin",
        password="delegated administrator password",
    )
    role = await _create_role(client, "delegated-admin")
    assert (
        await client.put(
            f"/api/v1/admin/roles/{role['id']}/permissions",
            json={"permission_codes": [ADMIN_USER_MANAGE_PERMISSION]},
        )
    ).status_code == 200
    assert (
        await client.put(
            f"/api/v1/admin/users/{delegated['id']}/roles",
            json={"role_codes": ["delegated-admin"]},
        )
    ).status_code == 200

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as delegated_client:
        _, login_response = await login_client(
            delegated_client,
            username="delegated-admin",
            password="delegated administrator password",
        )
        assert login_response.status_code == 200
        assert login_response.json()["user"]["permissions"] == [ADMIN_USER_MANAGE_PERMISSION]
        assert (await delegated_client.get("/api/v1/admin/users")).status_code == 200

        missing_csrf = await delegated_client.post(
            "/api/v1/admin/users",
            json={"username": "created-without-csrf", "password": "a secure test password"},
        )
        assert missing_csrf.status_code == 403
        delegated_client.headers["X-CSRF-Token"] = login_response.json()["csrf_token"]

        created = await _create_user(
            delegated_client,
            username="created-by-delegate",
            password="created by delegate password",
        )
        assert created["roles"] == []
        assert (
            await delegated_client.post(
                "/api/v1/admin/roles",
                json={"code": "self-escalation", "name": "Self escalation"},
            )
        ).status_code == 403
        assert (
            await delegated_client.patch(
                f"/api/v1/admin/users/{owner['id']}/status",
                json={"status": "DISABLED"},
            )
        ).status_code == 403

    async with AsyncClient(transport=transport, base_url="http://testserver") as plain_client:
        _, plain_login = await login_client(
            plain_client,
            username="created-by-delegate",
            password="created by delegate password",
        )
        assert plain_login.status_code == 200
        assert (await plain_client.get("/api/v1/admin/users")).status_code == 403


async def test_rbac_management_rejects_conflicts_and_unknown_bindings(
    client: AsyncClient,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _login_owner(client)
    user = await _create_user(client)
    duplicate = await client.post(
        "/api/v1/admin/users",
        json={"username": "MANAGED-ADMIN", "password": "another secure password"},
    )
    assert duplicate.status_code == 409

    role = await _create_role(client, "limited-role")
    unknown_permission = await client.put(
        f"/api/v1/admin/roles/{role['id']}/permissions",
        json={"permission_codes": ["unknown.permission"]},
    )
    assert unknown_permission.status_code == 422
    unknown_role = await client.put(
        f"/api/v1/admin/users/{user['id']}/roles",
        json={"role_codes": ["unknown-role"]},
    )
    assert unknown_role.status_code == 422


async def test_system_permission_catalog_sync_is_idempotent(
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    async with app.state.database.session_factory() as database:
        await database.execute(
            delete(RbacPermission).where(RbacPermission.code == "directory.read")
        )
        await ensure_system_permissions(database)
        await database.flush()
        await ensure_system_permissions(database)
        await database.commit()

    async with app.state.database.session_factory() as database:
        matching = await database.scalar(
            select(func.count())
            .select_from(RbacPermission)
            .where(RbacPermission.code == "directory.read")
        )
    assert matching == 1
