from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.dependencies import (
    CurrentPrincipalDependency,
    require_management_request,
    require_permission,
)
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.core.logging import get_logger
from wechat_bot.directory.schemas import (
    ChatroomList,
    ContactList,
    DirectorySyncResult,
    MembershipDepartureRequest,
    MembershipList,
    MembershipSyncResult,
    MembershipView,
)
from wechat_bot.directory.service import (
    DirectoryCredentialError,
    DirectoryMembershipConflictError,
    DirectoryNotFoundError,
    DirectoryService,
)
from wechat_bot.gewe.client import GeWeClientError

router = APIRouter(
    prefix="/api/v1/directory",
    tags=["Directory"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission("directory.read")),
    ],
)
logger = get_logger(component="directory_api")


@router.get("/bot-accounts/{bot_account_id}/contacts", response_model=ContactList)
async def list_contacts(
    bot_account_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ContactList:
    try:
        return await _service(request).list_contacts(
            session,
            bot_account_id,
            limit=limit,
            offset=offset,
        )
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc


@router.get("/bot-accounts/{bot_account_id}/chatrooms", response_model=ChatroomList)
async def list_chatrooms(
    bot_account_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatroomList:
    try:
        return await _service(request).list_chatrooms(
            session,
            bot_account_id,
            limit=limit,
            offset=offset,
        )
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc


@router.get("/chatrooms/{chatroom_id}/members", response_model=MembershipList)
async def list_chatroom_members(
    chatroom_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    include_left: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MembershipList:
    try:
        return await _service(request).list_memberships(
            session,
            chatroom_id,
            include_left=include_left,
            limit=limit,
            offset=offset,
        )
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc


@router.post(
    "/bot-accounts/{bot_account_id}/sync",
    response_model=DirectorySyncResult,
    dependencies=[Depends(require_permission("directory.sync"))],
)
async def sync_directory(
    bot_account_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DirectorySyncResult:
    try:
        result = await _service(request).sync_contacts(session, bot_account_id)
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc
    except DirectoryCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        await session.rollback()
        logger.warning(
            "directory_sync_upstream_failed",
            bot_account_id=str(bot_account_id),
            error_type=type(exc).__name__,
            retryable=exc.retryable,
        )
        raise _upstream_error(exc.retryable) from exc
    await session.commit()
    return result


@router.post(
    "/chatrooms/{chatroom_id}/sync-members",
    response_model=MembershipSyncResult,
    dependencies=[Depends(require_permission("directory.sync"))],
)
async def sync_chatroom_members(
    chatroom_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MembershipSyncResult:
    try:
        result = await _service(request).sync_chatroom_members(session, chatroom_id)
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc
    except DirectoryCredentialError as exc:
        raise _credential_unavailable() from exc
    except GeWeClientError as exc:
        raise _upstream_error(exc.retryable) from exc
    await session.commit()
    return result


@router.post(
    "/chatrooms/{chatroom_id}/memberships/{membership_id}/mark-left",
    response_model=MembershipView,
    dependencies=[
        Depends(require_permission("directory.sync")),
        Depends(require_permission("policy.write")),
    ],
)
async def mark_chatroom_membership_left(
    chatroom_id: UUID,
    membership_id: UUID,
    payload: MembershipDepartureRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: CurrentPrincipalDependency,
) -> MembershipView:
    try:
        result = await _service(request).mark_membership_left(
            session,
            chatroom_id,
            membership_id,
            membership_epoch=payload.membership_epoch,
            reason=payload.reason,
            actor=actor,
        )
    except DirectoryNotFoundError as exc:
        raise _not_found(exc.resource) from exc
    except DirectoryMembershipConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return result


def _service(request: Request) -> DirectoryService:
    settings: Settings = request.app.state.settings
    return DirectoryService(
        cipher=CredentialCipher.from_settings(settings),
        contacts_cache_poll_attempts=settings.directory_contacts_cache_poll_attempts,
        contacts_cache_poll_interval_seconds=(
            settings.directory_contacts_cache_poll_interval_seconds
        ),
    )


def _not_found(resource: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} not found",
    )


def _credential_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="connection credential unavailable",
    )


def _upstream_error(retryable: bool) -> HTTPException:
    return HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE if retryable else status.HTTP_502_BAD_GATEWAY
        ),
        detail="GeWe directory sync failed",
    )
