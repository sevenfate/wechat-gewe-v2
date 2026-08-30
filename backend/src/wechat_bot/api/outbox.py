from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from wechat_bot.auth.constants import OUTBOX_MANAGE_PERMISSION, OUTBOX_READ_PERMISSION
from wechat_bot.auth.dependencies import (
    CurrentPrincipalDependency,
    DatabaseSessionDependency,
    require_management_request,
    require_permission,
)
from wechat_bot.db.models import OutboxStatus
from wechat_bot.outbox.schemas import (
    OutboxManualActionRequest,
    OutboxMessageList,
    OutboxMessageView,
    OutboxReconcileRequest,
)
from wechat_bot.outbox.service import (
    OutboxMessageNotFoundError,
    OutboxService,
    OutboxStateConflictError,
)

router = APIRouter(
    prefix="/api/v1/outbox",
    tags=["Outbox"],
    dependencies=[Depends(require_management_request)],
)
service = OutboxService()


@router.get(
    "",
    response_model=OutboxMessageList,
    dependencies=[Depends(require_permission(OUTBOX_READ_PERMISSION))],
)
async def list_outbox_messages(
    database: DatabaseSessionDependency,
    bot_account_id: UUID | None = None,
    message_status: Annotated[OutboxStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OutboxMessageList:
    messages, total = await service.list_messages(
        database,
        bot_account_id=bot_account_id,
        status=message_status,
        limit=limit,
        offset=offset,
    )
    return OutboxMessageList(
        items=[OutboxMessageView.model_validate(message) for message in messages],
        total=total,
    )


@router.get(
    "/{message_id}",
    response_model=OutboxMessageView,
    dependencies=[Depends(require_permission(OUTBOX_READ_PERMISSION))],
)
async def get_outbox_message(
    message_id: UUID,
    database: DatabaseSessionDependency,
) -> OutboxMessageView:
    try:
        message = await service.get_message(database, message_id)
    except OutboxMessageNotFoundError as exc:
        raise _not_found() from exc
    return OutboxMessageView.model_validate(message)


@router.post(
    "/{message_id}/cancel",
    response_model=OutboxMessageView,
    dependencies=[Depends(require_permission(OUTBOX_MANAGE_PERMISSION))],
)
async def cancel_outbox_message(
    message_id: UUID,
    payload: OutboxManualActionRequest,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> OutboxMessageView:
    try:
        message = await service.cancel_message(
            database,
            message_id,
            actor=actor,
            reason=payload.reason,
        )
        await database.commit()
    except OutboxMessageNotFoundError as exc:
        raise _not_found() from exc
    except OutboxStateConflictError as exc:
        raise _conflict(exc) from exc
    return OutboxMessageView.model_validate(message)


@router.post(
    "/{message_id}/reconcile",
    response_model=OutboxMessageView,
    dependencies=[Depends(require_permission(OUTBOX_MANAGE_PERMISSION))],
)
async def reconcile_unknown_outbox_message(
    message_id: UUID,
    payload: OutboxReconcileRequest,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> OutboxMessageView:
    try:
        message = await service.reconcile_unknown(
            database,
            message_id,
            actor=actor,
            resolution=payload.resolution,
            reason=payload.reason,
        )
        await database.commit()
    except OutboxMessageNotFoundError as exc:
        raise _not_found() from exc
    except OutboxStateConflictError as exc:
        raise _conflict(exc) from exc
    return OutboxMessageView.model_validate(message)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="outbox message not found",
    )


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
