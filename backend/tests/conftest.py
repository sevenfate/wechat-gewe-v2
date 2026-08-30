from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from wechat_bot.core.config import Environment, Settings
from wechat_bot.db.base import Base
from wechat_bot.db.registry import load_all_models
from wechat_bot.main import create_app

load_all_models()

TEST_BOOTSTRAP_TOKEN = "test-bootstrap-token-with-at-least-32-characters"
TEST_OWNER_USERNAME = "test-owner"
TEST_OWNER_PASSWORD = "test owner password 123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "test.db"
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        public_base_url="http://testserver",
        local_master_key_path=tmp_path / "master.key",
        auth_bootstrap_token=TEST_BOOTSTRAP_TOKEN,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with app.router.lifespan_context(app):
        database = app.state.database
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
            yield test_client


@pytest.fixture
async def admin_client(client: AsyncClient) -> AsyncClient:
    bootstrap = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": TEST_BOOTSTRAP_TOKEN},
        json={
            "username": TEST_OWNER_USERNAME,
            "display_name": "Test Owner",
            "password": TEST_OWNER_PASSWORD,
        },
    )
    assert bootstrap.status_code == 201
    csrf = await client.get("/api/auth/csrf")
    assert csrf.status_code == 200
    csrf_token = csrf.json()["csrf_token"]
    login = await client.post(
        "/api/auth/login",
        headers={"X-CSRF-Token": csrf_token},
        json={"username": TEST_OWNER_USERNAME, "password": TEST_OWNER_PASSWORD},
    )
    assert login.status_code == 200
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    return client
