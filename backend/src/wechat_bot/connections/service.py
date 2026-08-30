from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.connections.schemas import ConnectionCreate, ConnectionView
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.models import (
    CallbackManagementMode,
    ConnectionStatus,
    GeweConnection,
    Workspace,
)
from wechat_bot.gewe.client import GeWeClient
from wechat_bot.policy.fence import lock_authorization_fence


class ConnectionNotFoundError(LookupError):
    pass


class DuplicateConnectionError(ValueError):
    pass


class SingleWorkspaceViolationError(ValueError):
    pass


class CallbackModeError(ValueError):
    pass


async def assert_single_workspace(session: AsyncSession) -> None:
    workspace_count = await session.scalar(select(func.count()).select_from(Workspace))
    if (workspace_count or 0) > 1:
        raise SingleWorkspaceViolationError(
            "database contains multiple workspaces, but this release supports exactly one"
        )


@dataclass(slots=True)
class ConnectionManager:
    settings: Settings
    cipher: CredentialCipher

    async def create(
        self,
        session: AsyncSession,
        payload: ConnectionCreate,
    ) -> ConnectionView:
        workspace = await self._ensure_workspace(
            session,
            slug=payload.workspace_slug,
            name=payload.workspace_name,
        )
        existing = await session.scalar(
            select(GeweConnection.id).where(
                GeweConnection.workspace_id == workspace.id,
                GeweConnection.name == payload.name,
            )
        )
        if existing is not None:
            raise DuplicateConnectionError("connection name already exists")

        token = payload.token.get_secret_value()
        if not token:
            raise ValueError("GeWe token cannot be empty")
        callback_secret = secrets.token_urlsafe(32)
        connection = GeweConnection(
            workspace_id=workspace.id,
            name=payload.name,
            api_base_url=str(payload.api_base_url).rstrip("/"),
            token_ciphertext=self.cipher.encrypt(token),
            token_fingerprint=self.cipher.fingerprint(token),
            callback_secret_ciphertext=self.cipher.encrypt(callback_secret),
            callback_secret_hash=hashlib.sha256(callback_secret.encode("utf-8")).hexdigest(),
            callback_mode=CallbackManagementMode.MANUAL,
            status=ConnectionStatus.ACTIVE,
        )
        session.add(connection)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateConnectionError("connection already exists") from exc
        return self.to_view(connection)

    async def list(self, session: AsyncSession) -> tuple[list[ConnectionView], int]:
        connections = list(
            await session.scalars(
                select(GeweConnection).order_by(
                    GeweConnection.created_at.asc(), GeweConnection.id.asc()
                )
            )
        )
        total = await session.scalar(select(func.count()).select_from(GeweConnection))
        return [self.to_view(item) for item in connections], total or 0

    async def get(self, session: AsyncSession, connection_id: UUID) -> GeweConnection:
        connection = await session.get(GeweConnection, connection_id)
        if connection is None:
            raise ConnectionNotFoundError
        return connection

    async def rotate_token(
        self,
        session: AsyncSession,
        connection_id: UUID,
        token: str,
    ) -> ConnectionView:
        if not token:
            raise ValueError("GeWe token cannot be empty")
        connection = await self.get(session, connection_id)
        connection.token_ciphertext = self.cipher.encrypt(token)
        connection.token_fingerprint = self.cipher.fingerprint(token)
        connection.last_callback_error = None
        await session.flush()
        return self.to_view(connection)

    async def change_mode(
        self,
        session: AsyncSession,
        connection_id: UUID,
        mode: CallbackManagementMode,
    ) -> ConnectionView:
        connection = await self.get(session, connection_id)
        connection.callback_mode = mode
        await session.flush()
        return self.to_view(connection)

    async def change_status(
        self,
        session: AsyncSession,
        connection_id: UUID,
        connection_status: ConnectionStatus,
    ) -> ConnectionView:
        connection = await self.get(session, connection_id)
        await lock_authorization_fence(
            session,
            connection.workspace_id,
            shared=False,
        )
        connection.status = connection_status
        await session.flush()
        return self.to_view(connection)

    async def apply_managed_callback(
        self,
        session: AsyncSession,
        connection_id: UUID,
    ) -> ConnectionView:
        connection = await self.get(session, connection_id)
        if connection.callback_mode is not CallbackManagementMode.PLATFORM_MANAGED:
            raise CallbackModeError("callback can only be applied in PLATFORM_MANAGED mode")

        callback_url = self._callback_url(connection)
        token = self.cipher.decrypt(connection.token_ciphertext)
        async with GeWeClient(base_url=connection.api_base_url, token=token) as client:
            await client.set_callback(callback_url)

        connection.callback_expected_url_ciphertext = self.cipher.encrypt(callback_url)
        connection.last_callback_error = None
        await session.flush()
        return self.to_view(connection)

    def to_view(self, connection: GeweConnection) -> ConnectionView:
        expected_url = (
            self.cipher.decrypt(connection.callback_expected_url_ciphertext)
            if connection.callback_expected_url_ciphertext is not None
            else None
        )
        return ConnectionView(
            id=connection.id,
            workspace_id=connection.workspace_id,
            name=connection.name,
            api_base_url=connection.api_base_url,
            token_fingerprint=connection.token_fingerprint,
            callback_mode=connection.callback_mode,
            callback_url=self._callback_url(connection),
            callback_expected_url=expected_url,
            callback_verified_at=connection.callback_verified_at,
            last_callback_at=connection.last_callback_at,
            last_callback_error=connection.last_callback_error,
            status=connection.status,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
        )

    def _callback_url(self, connection: GeweConnection) -> str:
        callback_secret = self.cipher.decrypt(connection.callback_secret_ciphertext)
        return f"{self.settings.public_base_url}/webhooks/gewe/{callback_secret}"

    @staticmethod
    async def _ensure_workspace(
        session: AsyncSession,
        *,
        slug: str,
        name: str,
    ) -> Workspace:
        workspace = await session.scalar(select(Workspace).where(Workspace.slug == slug))
        if workspace is not None:
            return workspace
        existing_workspace = await session.scalar(
            select(Workspace).order_by(Workspace.created_at, Workspace.id).limit(1)
        )
        if existing_workspace is not None:
            raise SingleWorkspaceViolationError(
                "this deployment supports one workspace; use the existing workspace slug"
            )
        workspace = Workspace(slug=slug, name=name)
        try:
            async with session.begin_nested():
                session.add(workspace)
                await session.flush()
        except IntegrityError as exc:
            raced_workspace = await session.scalar(
                select(Workspace).order_by(Workspace.created_at, Workspace.id).limit(1)
            )
            if raced_workspace is not None and raced_workspace.slug == slug:
                return raced_workspace
            raise SingleWorkspaceViolationError(
                "this deployment supports one workspace; use the existing workspace slug"
            ) from exc
        return workspace
