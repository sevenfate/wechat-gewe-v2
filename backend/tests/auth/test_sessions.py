from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Response
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.api.auth import set_session_cookies
from wechat_bot.auth.constants import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from wechat_bot.auth.service import IssuedSession, invalidate_user_sessions
from wechat_bot.auth.tokens import hash_token
from wechat_bot.core.config import Environment, Settings
from wechat_bot.db.auth_models import AdminSession, AdminUser, AdminUserStatus
from wechat_bot.db.base import utc_now

from .helpers import OWNER_PASSWORD, OWNER_USERNAME, login_client


async def test_login_requires_pre_auth_double_submit_csrf(
    client: AsyncClient,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    missing = await client.post(
        "/api/auth/login",
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert missing.status_code == 403

    csrf_response = await client.get("/api/auth/csrf")
    assert csrf_response.status_code == 200
    mismatched = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": "different-token"},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert mismatched.status_code == 403


async def test_login_issues_hardened_cookie_and_stores_only_hashes(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    _, response = await login_client(client)

    assert response.status_code == 200
    assert response.json()["user"]["roles"] == ["owner"]
    cookie_headers = response.headers.get_list("set-cookie")
    session_header = next(
        header for header in cookie_headers if header.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in session_header
    assert "SameSite=strict" in session_header
    assert "Secure" not in session_header

    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
    assert session_token is not None
    assert csrf_token is not None
    async with app.state.database.session_factory() as database:
        session_record = await database.scalar(select(AdminSession))
    assert session_record is not None
    assert session_record.token_hash == hash_token(session_token)
    assert session_record.csrf_token_hash == hash_token(csrf_token)
    assert session_record.token_hash != session_token
    assert session_record.csrf_token_hash != csrf_token


def test_production_session_cookie_is_secure() -> None:
    settings = Settings(
        environment=Environment.PRODUCTION,
        database_url="postgresql+psycopg://localhost/wechat",
        public_base_url="https://bot.example.com",
        master_key=Fernet.generate_key().decode("ascii"),
    )
    now = utc_now()
    issued_session = IssuedSession(
        session_id=uuid4(),
        token="session-token",
        csrf_token="csrf-token",
        idle_expires_at=now + timedelta(minutes=30),
        absolute_expires_at=now + timedelta(hours=12),
    )
    response = Response()

    set_session_cookies(response, issued_session, settings)

    session_header = next(
        header
        for header in response.headers.getlist("set-cookie")
        if header.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "Secure" in session_header
    assert "HttpOnly" in session_header
    assert "SameSite=strict" in session_header


async def test_logout_requires_session_bound_csrf_and_revokes_session(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    _, login_response = await login_client(client)
    assert login_response.status_code == 200
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    csrf_token = login_response.json()["csrf_token"]
    assert session_token is not None

    forged = await client.post(
        "/api/auth/logout",
        headers={
            "Cookie": (
                f"{SESSION_COOKIE_NAME}={session_token}; {CSRF_COOKIE_NAME}=forged-csrf-token"
            ),
            "X-CSRF-Token": "forged-csrf-token",
        },
    )
    assert forged.status_code == 403
    assert (await client.get("/api/auth/me")).status_code == 200

    logout_response = await client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert logout_response.status_code == 200
    assert SESSION_COOKIE_NAME not in client.cookies
    assert (await client.get("/api/auth/me")).status_code == 401

    async with app.state.database.session_factory() as database:
        session_record = await database.scalar(select(AdminSession))
    assert session_record is not None
    assert session_record.revoked_at is not None
    assert session_record.revoked_reason == "logout"


@pytest.mark.parametrize(
    ("expiry_field", "expected_reason"),
    [("idle_expires_at", "idle_expired"), ("absolute_expires_at", "absolute_expired")],
)
async def test_expired_session_is_rejected_and_marked_revoked(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
    expiry_field: str,
    expected_reason: str,
) -> None:
    del bootstrapped_owner
    _, login_response = await login_client(client)
    assert login_response.status_code == 200
    async with app.state.database.session_factory() as database:
        session_record = await database.scalar(select(AdminSession))
        assert session_record is not None
        setattr(session_record, expiry_field, utc_now() - timedelta(seconds=1))
        await database.commit()

    assert (await client.get("/api/auth/me")).status_code == 401
    async with app.state.database.session_factory() as database:
        session_record = await database.scalar(select(AdminSession))
    assert session_record is not None
    assert session_record.revoked_reason == expected_reason


async def test_auth_version_change_invalidates_existing_session(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    _, login_response = await login_client(client)
    assert login_response.status_code == 200
    async with app.state.database.session_factory() as database:
        user = await database.scalar(select(AdminUser))
        assert user is not None
        user.auth_version += 1
        await database.commit()

    assert (await client.get("/api/auth/me")).status_code == 401
    async with app.state.database.session_factory() as database:
        session_record = await database.scalar(select(AdminSession))
    assert session_record is not None
    assert session_record.revoked_reason == "auth_version_changed"


async def test_disabling_user_revokes_all_sessions_and_increments_version(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    _, login_response = await login_client(client)
    assert login_response.status_code == 200
    async with app.state.database.session_factory() as database:
        user = await database.scalar(select(AdminUser))
        assert user is not None
        original_version = user.auth_version
        await invalidate_user_sessions(
            database,
            user.id,
            reason="user_disabled",
            disable_user=True,
        )

    assert (await client.get("/api/auth/me")).status_code == 401
    async with app.state.database.session_factory() as database:
        user = await database.scalar(select(AdminUser))
        session_record = await database.scalar(select(AdminSession))
    assert user is not None
    assert user.status == AdminUserStatus.DISABLED
    assert user.auth_version == original_version + 1
    assert session_record is not None
    assert session_record.revoked_reason == "user_disabled"
