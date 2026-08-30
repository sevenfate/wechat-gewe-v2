from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.dependencies import require_management_request, require_permission
from wechat_bot.connections.schemas import (
    CallbackApplyResult,
    ConnectionCreate,
    ConnectionList,
    ConnectionModeUpdate,
    ConnectionStatusUpdate,
    ConnectionTokenUpdate,
    ConnectionView,
)
from wechat_bot.connections.service import (
    CallbackModeError,
    ConnectionManager,
    ConnectionNotFoundError,
    DuplicateConnectionError,
    SingleWorkspaceViolationError,
)
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher

router = APIRouter(
    prefix="/api/v1/connections",
    tags=["GeWe connections"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission("connection.read")),
    ],
)


@router.post(
    "",
    response_model=ConnectionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("connection.write"))],
)
async def create_connection(
    payload: ConnectionCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionView:
    manager = _manager(request)
    try:
        result = await manager.create(session, payload)
    except (DuplicateConnectionError, SingleWorkspaceViolationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return result


@router.get("", response_model=ConnectionList)
async def list_connections(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionList:
    items, total = await _manager(request).list(session)
    return ConnectionList(items=items, total=total)


@router.get("/{connection_id}", response_model=ConnectionView)
async def get_connection(
    connection_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionView:
    manager = _manager(request)
    try:
        connection = await manager.get(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise _not_found() from exc
    return manager.to_view(connection)


@router.put(
    "/{connection_id}/token",
    response_model=ConnectionView,
    dependencies=[Depends(require_permission("connection.write"))],
)
async def rotate_connection_token(
    connection_id: UUID,
    payload: ConnectionTokenUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionView:
    try:
        result = await _manager(request).rotate_token(
            session,
            connection_id,
            payload.token.get_secret_value(),
        )
    except ConnectionNotFoundError as exc:
        raise _not_found() from exc
    await session.commit()
    return result


@router.put(
    "/{connection_id}/callback-mode",
    response_model=ConnectionView,
    dependencies=[Depends(require_permission("connection.write"))],
)
async def update_callback_mode(
    connection_id: UUID,
    payload: ConnectionModeUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionView:
    try:
        result = await _manager(request).change_mode(
            session,
            connection_id,
            payload.callback_mode,
        )
    except ConnectionNotFoundError as exc:
        raise _not_found() from exc
    await session.commit()
    return result


@router.put(
    "/{connection_id}/status",
    response_model=ConnectionView,
    dependencies=[Depends(require_permission("connection.write"))],
)
async def update_connection_status(
    connection_id: UUID,
    payload: ConnectionStatusUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ConnectionView:
    try:
        result = await _manager(request).change_status(
            session,
            connection_id,
            payload.status,
        )
    except ConnectionNotFoundError as exc:
        raise _not_found() from exc
    await session.commit()
    return result


@router.post(
    "/{connection_id}/callback/apply",
    response_model=CallbackApplyResult,
    dependencies=[Depends(require_permission("connection.write"))],
)
async def apply_managed_callback(
    connection_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallbackApplyResult:
    try:
        result = await _manager(request).apply_managed_callback(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise _not_found() from exc
    except CallbackModeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return CallbackApplyResult(connection=result, applied=True)


def _manager(request: Request) -> ConnectionManager:
    settings: Settings = request.app.state.settings
    return ConnectionManager(settings=settings, cipher=CredentialCipher.from_settings(settings))


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="connection not found",
    )
