from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.auth.passwords import password_manager
from wechat_bot.db.auth_models import (
    AdminUser,
    AdminUserStatus,
    AuthEventOutcome,
    AuthLoginThrottle,
    AuthSecurityEvent,
    LoginThrottleDimension,
)

from .helpers import OWNER_PASSWORD, OWNER_USERNAME


async def _csrf_token(client: AsyncClient) -> str:
    response = await client.get("/api/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def test_login_is_rate_limited_by_account_and_source_and_audited(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    csrf_token = await _csrf_token(client)
    for _ in range(2):
        response = await client.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf_token},
            json={"username": OWNER_USERNAME, "password": "wrong password"},
        )
        assert response.status_code == 401

    blocked = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    async with app.state.database.session_factory() as database:
        throttle_records = (await database.scalars(select(AuthLoginThrottle))).all()
        security_events = (await database.scalars(select(AuthSecurityEvent))).all()
    assert {record.dimension for record in throttle_records} == {
        LoginThrottleDimension.ACCOUNT,
        LoginThrottleDimension.SOURCE,
    }
    assert all(record.failure_count == 2 for record in throttle_records)
    assert all(record.blocked_until is not None for record in throttle_records)
    assert [event.outcome for event in security_events[-3:]] == [
        AuthEventOutcome.DENIED,
        AuthEventOutcome.DENIED,
        AuthEventOutcome.RATE_LIMITED,
    ]
    assert all("password" not in str(event.detail).lower() for event in security_events)


async def test_source_limit_blocks_a_different_valid_account(
    client: AsyncClient,
    app: FastAPI,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    async with app.state.database.session_factory() as database:
        database.add(
            AdminUser(
                username="second-admin",
                username_normalized="second-admin",
                display_name=None,
                password_hash=password_manager.hash("second account password"),
                status=AdminUserStatus.ACTIVE,
                auth_version=1,
            )
        )
        await database.commit()

    csrf_token = await _csrf_token(client)
    for index in range(2):
        response = await client.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf_token},
            json={"username": f"unknown-{index}", "password": "wrong password"},
        )
        assert response.status_code == 401

    blocked = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": "second-admin", "password": "second account password"},
    )
    assert blocked.status_code == 429


async def test_invalid_username_format_still_counts_toward_source_limit(
    client: AsyncClient,
    bootstrapped_owner: dict[str, str],
) -> None:
    del bootstrapped_owner
    csrf_token = await _csrf_token(client)
    for username in ("invalid user", "another invalid user"):
        response = await client.post(
            "/api/auth/login",
            headers={"X-CSRF-Token": csrf_token},
            json={"username": username, "password": "wrong password"},
        )
        assert response.status_code == 401

    blocked = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": OWNER_USERNAME, "password": OWNER_PASSWORD},
    )
    assert blocked.status_code == 429
