from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import AsyncClient

from wechat_bot.auth.dependencies import require_owner, require_permission
from wechat_bot.auth.service import AuthPrincipal
from wechat_bot.core.config import Environment, Settings
from wechat_bot.main import create_app

from .helpers import BOOTSTRAP_TOKEN, OWNER_PASSWORD, OWNER_USERNAME

import_module("wechat_bot.db.auth_models")

OwnerDependency = Annotated[AuthPrincipal, Depends(require_owner)]
DirectoryReadDependency = Annotated[
    AuthPrincipal,
    Depends(require_permission("directory.read")),
]
DirectoryWriteDependency = Annotated[
    AuthPrincipal,
    Depends(require_permission("directory.write")),
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    database_path = tmp_path / "auth-test.db"
    return Settings(
        environment=Environment.TEST,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        public_base_url="http://testserver",
        local_master_key_path=tmp_path / "master.key",
        auth_bootstrap_token=BOOTSTRAP_TOKEN,
        auth_session_idle_seconds=300,
        auth_session_absolute_seconds=3_600,
        auth_login_window_seconds=300,
        auth_login_max_failures=2,
        auth_login_block_seconds=120,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    application = create_app(settings)

    @application.get("/test/owner")
    async def owner_route(principal: OwnerDependency) -> dict[str, str]:
        return {"username": principal.username}

    @application.get("/test/directory-read")
    async def directory_read_route(principal: DirectoryReadDependency) -> dict[str, str]:
        return {"username": principal.username}

    @application.get("/test/directory-write")
    async def directory_write_route(principal: DirectoryWriteDependency) -> dict[str, str]:
        return {"username": principal.username}

    return application


@pytest.fixture
async def bootstrapped_owner(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/auth/bootstrap",
        headers={"X-Bootstrap-Token": BOOTSTRAP_TOKEN},
        json={
            "username": OWNER_USERNAME,
            "display_name": "Platform Owner",
            "password": OWNER_PASSWORD,
        },
    )
    assert response.status_code == 201
    return {"username": OWNER_USERNAME, "password": OWNER_PASSWORD}
