from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.auth.dependencies import require_permission
from wechat_bot.auth.passwords import password_manager
from wechat_bot.db.auth_models import (
    AdminUser,
    AdminUserStatus,
    RbacPermission,
    RbacRole,
    RbacRolePermission,
    RbacUserRole,
)

from .helpers import login_client

OPERATOR_USERNAME = "directory-operator"
OPERATOR_PASSWORD = "operator secure password"


async def _create_directory_operator(app: FastAPI) -> None:
    async with app.state.database.session_factory() as database:
        user = AdminUser(
            username=OPERATOR_USERNAME,
            username_normalized=OPERATOR_USERNAME,
            display_name="Directory Operator",
            password_hash=password_manager.hash(OPERATOR_PASSWORD),
            status=AdminUserStatus.ACTIVE,
            auth_version=1,
        )
        role = RbacRole(code="operator", name="Operator", active=True, is_system=True)
        permission = await database.scalar(
            select(RbacPermission).where(RbacPermission.code == "directory.read")
        )
        assert permission is not None
        database.add_all([user, role])
        await database.flush()
        database.add_all(
            [
                RbacUserRole(user_id=user.id, role_id=role.id),
                RbacRolePermission(role_id=role.id, permission_id=permission.id),
            ]
        )
        await database.commit()


async def test_owner_bypasses_named_permissions(
    client: AsyncClient,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    _, login_response = await login_client(client)
    assert login_response.status_code == 200

    assert (await client.get("/test/owner")).status_code == 200
    assert (await client.get("/test/directory-read")).status_code == 200
    assert (await client.get("/test/directory-write")).status_code == 200


async def test_non_owner_gets_only_exact_permission_code(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _create_directory_operator(app)
    _, login_response = await login_client(
        client,
        username=OPERATOR_USERNAME,
        password=OPERATOR_PASSWORD,
    )
    assert login_response.status_code == 200
    assert login_response.json()["user"]["permissions"] == ["directory.read"]

    assert (await client.get("/test/owner")).status_code == 403
    assert (await client.get("/test/directory-read")).status_code == 200
    assert (await client.get("/test/directory-write")).status_code == 403


async def test_inactive_role_stops_authorizing_immediately(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    await _create_directory_operator(app)
    _, login_response = await login_client(
        client,
        username=OPERATOR_USERNAME,
        password=OPERATOR_PASSWORD,
    )
    assert login_response.status_code == 200
    assert (await client.get("/test/directory-read")).status_code == 200

    async with app.state.database.session_factory() as database:
        role = await database.scalar(select(RbacRole).where(RbacRole.code == "operator"))
        assert role is not None
        role.active = False
        await database.commit()

    assert (await client.get("/test/directory-read")).status_code == 403


def test_permission_dependency_rejects_wildcards_and_invalid_codes() -> None:
    with pytest.raises(ValueError, match="not valid"):
        require_permission("directory.*")

    with pytest.raises(ValueError, match="not valid"):
        require_permission("Directory.Read")
