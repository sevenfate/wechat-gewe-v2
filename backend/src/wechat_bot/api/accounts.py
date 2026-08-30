from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.accounts.schemas import (
    BotAccountList,
    BotAccountStatusUpdate,
    BotAccountView,
    LoginCheckRequest,
    LoginCheckResult,
    LoginQrCodeRequest,
    LoginQrCodeResult,
    ManualBotAccountCreate,
    OnlineCheckResult,
    ReconnectResult,
)
from wechat_bot.accounts.service import (
    AccountCredentialError,
    AccountNotFoundError,
    AccountService,
    AccountStateError,
)
from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.dependencies import require_management_request, require_permission
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.gewe.client import GeWeClientError

router = APIRouter(
    prefix="/api/v1",
    tags=["Bot accounts"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission("account.read")),
    ],
)


@router.get("/bot-accounts", response_model=BotAccountList)
async def list_bot_accounts(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    connection_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> BotAccountList:
    return await _service(request).list(
        session,
        connection_id=connection_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/connections/{connection_id}/bot-accounts",
    response_model=BotAccountView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("account.write"))],
)
async def register_bot_account(
    connection_id: UUID,
    payload: ManualBotAccountCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BotAccountView:
    try:
        account = await _service(request).register_manual(session, connection_id, payload)
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    await session.commit()
    return BotAccountView.model_validate(account)


@router.post(
    "/connections/{connection_id}/login/qr-code",
    response_model=LoginQrCodeResult,
    dependencies=[Depends(require_permission("account.write"))],
)
async def get_login_qr_code(
    connection_id: UUID,
    payload: LoginQrCodeRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginQrCodeResult:
    try:
        result = await _service(request).get_login_qr_code(session, connection_id, payload)
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    except AccountStateError as exc:
        raise _conflict(exc) from exc
    except AccountCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        raise _upstream(exc) from exc
    await session.commit()
    return result


@router.post(
    "/bot-accounts/{account_id}/login/check",
    response_model=LoginCheckResult,
    dependencies=[Depends(require_permission("account.write"))],
)
async def check_login(
    account_id: UUID,
    payload: LoginCheckRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginCheckResult:
    try:
        result = await _service(request).check_login(session, account_id, payload)
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    except AccountStateError as exc:
        raise _conflict(exc) from exc
    except AccountCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        raise _upstream(exc) from exc
    await session.commit()
    return result


@router.post(
    "/bot-accounts/{account_id}/check-online",
    response_model=OnlineCheckResult,
    dependencies=[Depends(require_permission("account.write"))],
)
async def check_online(
    account_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OnlineCheckResult:
    try:
        result = await _service(request).check_online(session, account_id)
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    except AccountStateError as exc:
        raise _conflict(exc) from exc
    except AccountCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        raise _upstream(exc) from exc
    await session.commit()
    return result


@router.post(
    "/bot-accounts/{account_id}/reconnect",
    response_model=ReconnectResult,
    dependencies=[Depends(require_permission("account.write"))],
)
async def reconnect(
    account_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReconnectResult:
    try:
        result = await _service(request).reconnect(session, account_id)
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    except AccountStateError as exc:
        raise _conflict(exc) from exc
    except AccountCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        raise _upstream(exc) from exc
    await session.commit()
    return result


@router.put(
    "/bot-accounts/{account_id}/disabled",
    response_model=BotAccountView,
    dependencies=[Depends(require_permission("account.write"))],
)
async def update_disabled(
    account_id: UUID,
    payload: BotAccountStatusUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BotAccountView:
    try:
        account = await _service(request).set_disabled(
            session,
            account_id,
            disabled=payload.disabled,
        )
    except AccountNotFoundError as exc:
        raise _not_found(exc) from exc
    await session.commit()
    return BotAccountView.model_validate(account)


def _service(request: Request) -> AccountService:
    settings: Settings = request.app.state.settings
    return AccountService(CredentialCipher.from_settings(settings))


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _credential_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="connection credential unavailable",
    )


def _upstream(exc: GeWeClientError) -> HTTPException:
    return HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_502_BAD_GATEWAY
        ),
        detail="GeWe account operation failed",
    )
