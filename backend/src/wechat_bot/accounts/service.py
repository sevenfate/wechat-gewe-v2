from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.accounts.schemas import (
    BotAccountList,
    BotAccountView,
    LoginCheckRequest,
    LoginCheckResult,
    LoginQrCodeRequest,
    LoginQrCodeResult,
    ManualBotAccountCreate,
    OnlineCheckResult,
    ReconnectResult,
)
from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import BotAccount, BotAccountStatus, GeweConnection
from wechat_bot.gewe.client import GeWeClient
from wechat_bot.gewe.schemas import LoginStatus, LoginStatusData
from wechat_bot.gewe.service import GeWeService
from wechat_bot.policy.fence import lock_authorization_fence

QR_LIFETIME_SECONDS = 150


class AccountNotFoundError(LookupError):
    pass


class AccountStateError(ValueError):
    pass


class AccountCredentialError(RuntimeError):
    pass


@dataclass(slots=True)
class AccountService:
    cipher: CredentialCipher

    async def list(
        self,
        session: AsyncSession,
        *,
        connection_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> BotAccountList:
        filters = []
        if connection_id is not None:
            filters.append(BotAccount.gewe_connection_id == connection_id)
        items = list(
            await session.scalars(
                select(BotAccount)
                .where(*filters)
                .order_by(BotAccount.created_at.asc(), BotAccount.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(BotAccount).where(*filters))
        return BotAccountList(
            items=[BotAccountView.model_validate(item) for item in items],
            total=total or 0,
        )

    async def register_manual(
        self,
        session: AsyncSession,
        connection_id: UUID,
        payload: ManualBotAccountCreate,
    ) -> BotAccount:
        await self._connection(session, connection_id)
        account = await session.scalar(
            select(BotAccount).where(
                BotAccount.gewe_connection_id == connection_id,
                BotAccount.app_id == payload.app_id,
            )
        )
        if account is None:
            account = BotAccount(
                gewe_connection_id=connection_id,
                app_id=payload.app_id,
                wxid=payload.wxid,
                note=payload.note,
                status=BotAccountStatus.OFFLINE,
            )
            session.add(account)
        else:
            if payload.wxid is not None:
                account.wxid = payload.wxid
            if payload.note is not None:
                account.note = payload.note
        await session.flush()
        return account

    async def get_login_qr_code(
        self,
        session: AsyncSession,
        connection_id: UUID,
        payload: LoginQrCodeRequest,
    ) -> LoginQrCodeResult:
        connection = await self._connection(session, connection_id)
        client = self._gewe_client(connection)
        async with client:
            qr = await GeWeService(client).get_login_qr_code(
                device_type=payload.device_type,
                region_id=payload.region_id,
                app_id=payload.app_id,
                proxy_ip=payload.proxy_ip,
                ttuid=payload.ttuid,
                aid=payload.aid,
            )
        account = await session.scalar(
            select(BotAccount).where(
                BotAccount.gewe_connection_id == connection_id,
                BotAccount.app_id == qr.app_id,
            )
        )
        if account is None:
            account = BotAccount(
                gewe_connection_id=connection_id,
                app_id=qr.app_id,
                status=BotAccountStatus.QR_PENDING,
            )
            session.add(account)
        elif account.status is BotAccountStatus.DISABLED:
            raise AccountStateError("disabled account cannot start login")
        now = utc_now()
        expires_at = now + timedelta(seconds=QR_LIFETIME_SECONDS)
        account.pending_login_uuid = qr.uuid
        account.qr_expires_at = expires_at
        account.status = BotAccountStatus.QR_PENDING
        account.last_status_error = None
        await session.flush()
        return LoginQrCodeResult(
            account=BotAccountView.model_validate(account),
            qr_data=qr.qr_data,
            qr_image_base64=qr.qr_image_base64,
            uuid=qr.uuid,
            expires_at=expires_at,
        )

    async def check_login(
        self,
        session: AsyncSession,
        account_id: UUID,
        payload: LoginCheckRequest,
    ) -> LoginCheckResult:
        account, connection = await self._account_connection(session, account_id)
        if account.status is BotAccountStatus.DISABLED:
            raise AccountStateError("disabled account cannot login")
        if not account.pending_login_uuid:
            raise AccountStateError("account has no pending login QR code")
        client = self._gewe_client(connection)
        async with client:
            result = await GeWeService(client).check_login(
                app_id=account.app_id,
                uuid=account.pending_login_uuid,
                auto_sliding=payload.auto_sliding,
                proxy_ip=payload.proxy_ip,
                captcha_code=payload.captcha_code,
            )
        self._apply_login_status(account, result)
        await session.flush()
        return LoginCheckResult(
            account=BotAccountView.model_validate(account),
            login_status=result.status,
            verification_url=result.verification_url,
        )

    async def check_online(
        self,
        session: AsyncSession,
        account_id: UUID,
    ) -> OnlineCheckResult:
        account, connection = await self._account_connection(session, account_id)
        if account.status is BotAccountStatus.DISABLED:
            raise AccountStateError("disabled account cannot be checked")
        client = self._gewe_client(connection)
        async with client:
            online = await GeWeService(client).check_online(app_id=account.app_id)
        now = utc_now()
        account.last_status_checked_at = now
        account.status = BotAccountStatus.ONLINE if online else BotAccountStatus.OFFLINE
        account.last_online_at = now if online else account.last_online_at
        account.last_status_error = None
        await session.flush()
        return OnlineCheckResult(
            account=BotAccountView.model_validate(account),
            online=online,
        )

    async def reconnect(
        self,
        session: AsyncSession,
        account_id: UUID,
    ) -> ReconnectResult:
        account, connection = await self._account_connection(session, account_id)
        if account.status is BotAccountStatus.DISABLED:
            raise AccountStateError("disabled account cannot reconnect")
        account.status = BotAccountStatus.RECONNECTING
        client = self._gewe_client(connection)
        async with client:
            result = await GeWeService(client).reconnect(app_id=account.app_id)
        if result is None:
            account.status = BotAccountStatus.RECONNECTING
        else:
            self._apply_login_status(account, result)
        await session.flush()
        return ReconnectResult(
            account=BotAccountView.model_validate(account),
            login_status=result.status if result is not None else None,
        )

    async def set_disabled(
        self,
        session: AsyncSession,
        account_id: UUID,
        *,
        disabled: bool,
    ) -> BotAccount:
        row = (
            await session.execute(
                select(BotAccount, GeweConnection)
                .join(
                    GeweConnection,
                    GeweConnection.id == BotAccount.gewe_connection_id,
                )
                .where(BotAccount.id == account_id)
            )
        ).one_or_none()
        if row is None:
            raise AccountNotFoundError("bot account not found")
        account = cast(BotAccount, row[0])
        connection = cast(GeweConnection, row[1])
        await lock_authorization_fence(
            session,
            connection.workspace_id,
            shared=False,
        )
        await session.refresh(account, with_for_update=True)
        account.status = BotAccountStatus.DISABLED if disabled else BotAccountStatus.OFFLINE
        if disabled:
            account.pending_login_uuid = None
            account.qr_expires_at = None
        await session.flush()
        return account

    def _apply_login_status(self, account: BotAccount, result: LoginStatusData) -> None:
        now = utc_now()
        account.last_status_checked_at = now
        account.last_status_error = None
        if result.expired_time is not None:
            account.qr_expires_at = now + timedelta(seconds=max(result.expired_time, 0))
        if result.head_image_url is not None:
            account.avatar_url = result.head_image_url
        if result.nickname is not None:
            account.nickname = result.nickname
        if result.status is LoginStatus.NOT_SCANNED:
            account.status = BotAccountStatus.QR_PENDING
        elif result.status is LoginStatus.SCANNED or result.verification_url is not None:
            account.status = BotAccountStatus.SCANNED
        elif result.status is LoginStatus.LOGGED_IN:
            if result.login_info is None:
                raise AccountStateError("login succeeded without account information")
            account.status = BotAccountStatus.ONLINE
            account.wxid = result.login_info.wxid
            account.nickname = result.login_info.nickname
            account.alias = result.login_info.alias
            account.logged_in_at = now
            account.last_online_at = now
            account.pending_login_uuid = None
            account.qr_expires_at = None

    async def _connection(
        self,
        session: AsyncSession,
        connection_id: UUID,
    ) -> GeweConnection:
        connection = await session.get(GeweConnection, connection_id)
        if connection is None:
            raise AccountNotFoundError("GeWe connection not found")
        return connection

    async def _account_connection(
        self,
        session: AsyncSession,
        account_id: UUID,
    ) -> tuple[BotAccount, GeweConnection]:
        row = (
            await session.execute(
                select(BotAccount, GeweConnection)
                .join(GeweConnection, GeweConnection.id == BotAccount.gewe_connection_id)
                .where(BotAccount.id == account_id)
            )
        ).one_or_none()
        if row is None:
            raise AccountNotFoundError("bot account not found")
        return row[0], row[1]

    def _gewe_client(self, connection: GeweConnection) -> GeWeClient:
        try:
            token = self.cipher.decrypt(connection.token_ciphertext)
        except CredentialDecryptionError as exc:
            raise AccountCredentialError("connection credential unavailable") from exc
        return GeWeClient(base_url=connection.api_base_url, token=token)
