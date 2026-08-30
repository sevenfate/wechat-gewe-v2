from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from wechat_bot.auth.passwords import password_manager
from wechat_bot.core.config import Settings
from wechat_bot.db.auth_models import (
    AdminUser,
    AdminUserStatus,
    AuthBootstrapState,
    AuthEventOutcome,
    AuthSecurityEvent,
)

from .helpers import BOOTSTRAP_TOKEN, OWNER_PASSWORD, OWNER_USERNAME


async def test_bootstrap_creates_only_argon2_owner_and_consumes_token(
    client: AsyncClient,
    app: FastAPI,
) -> None:
    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )

    assert response.status_code == 201
    assert "wechat_bot_session" not in client.cookies
    database = app.state.database
    async with database.session_factory() as session:
        user = await session.scalar(select(AdminUser))
        state = await session.get(AuthBootstrapState, 1)

    assert user is not None
    assert user.password_hash.startswith("$argon2id$")
    assert OWNER_PASSWORD not in user.password_hash
    assert password_manager.verify(user.password_hash, OWNER_PASSWORD)
    assert state is not None
    assert state.owner_user_id == user.id
    assert state.token_fingerprint != BOOTSTRAP_TOKEN

    async with database.session_factory() as session:
        user = await session.scalar(select(AdminUser))
        assert user is not None
        user.status = AdminUserStatus.DISABLED
        await session.commit()

    repeated = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": "replacement-owner", "password": "another secure password"},
    )
    assert repeated.status_code == 409


async def test_bootstrap_rejects_wrong_token_and_audits(
    client: AsyncClient,
    app: FastAPI,
) -> None:
    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": "wrong-token-that-is-still-long-enough"},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )

    assert response.status_code == 403
    async with app.state.database.session_factory() as session:
        user_count = await session.scalar(select(func.count(AdminUser.id)))
        event = await session.scalar(select(AuthSecurityEvent))
    assert user_count == 0
    assert event is not None
    assert event.outcome == AuthEventOutcome.DENIED
    assert event.detail == {"reason": "invalid_token"}


async def test_bootstrap_is_unavailable_without_explicit_configuration(
    client: AsyncClient,
    app: FastAPI,
    settings: Settings,
) -> None:
    settings.auth_bootstrap_token = None

    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )

    assert response.status_code == 503
    async with app.state.database.session_factory() as session:
        user_count = await session.scalar(select(func.count(AdminUser.id)))
    assert user_count == 0


async def test_bootstrap_requires_explicit_password(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": OWNER_USERNAME},
    )

    assert response.status_code == 422


async def test_bootstrap_rejects_invalid_username_without_server_error(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={"username": "invalid user", "password": OWNER_PASSWORD},
    )

    assert response.status_code == 422
