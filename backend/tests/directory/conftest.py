from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    CallbackManagementMode,
    ConnectionStatus,
    GeweConnection,
    Workspace,
)


@dataclass(frozen=True, slots=True)
class DirectorySeed:
    bot_account_id: UUID
    connection_id: UUID
    app_id: str
    token: str


@pytest.fixture
async def directory_seed(
    app: FastAPI,
    admin_client: AsyncClient,
    settings: Settings,
) -> DirectorySeed:
    del admin_client  # Keeps the application lifespan and authenticated client active.
    token = "directory-super-secret-token"
    cipher = CredentialCipher.from_settings(settings)
    database = app.state.database
    async with database.session_factory() as session:
        workspace = Workspace(name="Directory tests", slug="directory-tests")
        session.add(workspace)
        await session.flush()

        callback_secret = "directory-callback-secret"
        connection = GeweConnection(
            workspace_id=workspace.id,
            name="Directory GeWe",
            api_base_url="https://api.gewe.test",
            token_ciphertext=cipher.encrypt(token),
            token_fingerprint=cipher.fingerprint(token),
            callback_mode=CallbackManagementMode.MANUAL,
            callback_secret_ciphertext=cipher.encrypt(callback_secret),
            callback_secret_hash=hashlib.sha256(callback_secret.encode("utf-8")).hexdigest(),
            status=ConnectionStatus.ACTIVE,
        )
        session.add(connection)
        await session.flush()

        account = BotAccount(
            gewe_connection_id=connection.id,
            app_id="wx_app_directory",
            wxid="wxid_bot",
            nickname="Directory bot",
            status=BotAccountStatus.ONLINE,
        )
        session.add(account)
        await session.commit()
        return DirectorySeed(
            bot_account_id=account.id,
            connection_id=connection.id,
            app_id=account.app_id,
            token=token,
        )
