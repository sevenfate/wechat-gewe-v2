from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from wechat_bot.auth.dependencies import (
    DatabaseSessionDependency,
    require_management_request,
    require_permission,
)
from wechat_bot.db.models import ConversationType, InboxStatus
from wechat_bot.observability.schemas import MessageDetailView, MessageList, TraceView
from wechat_bot.observability.service import (
    MessageNotFoundError,
    ObservabilityService,
    TraceNotFoundError,
)

router = APIRouter(
    prefix="/api/v1",
    tags=["Messages and traces"],
    dependencies=[Depends(require_management_request)],
)
service = ObservabilityService()


@router.get(
    "/messages",
    response_model=MessageList,
    dependencies=[Depends(require_permission("message.read"))],
)
async def list_messages(
    database: DatabaseSessionDependency,
    bot_account_id: UUID | None = None,
    message_status: Annotated[InboxStatus | None, Query(alias="status")] = None,
    conversation_type: ConversationType | None = None,
    conversation_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageList:
    items, total = await service.list_messages(
        database,
        bot_account_id=bot_account_id,
        inbox_status=message_status,
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        limit=limit,
        offset=offset,
    )
    return MessageList(items=items, total=total)


@router.get(
    "/messages/{event_id}",
    response_model=MessageDetailView,
    dependencies=[Depends(require_permission("message.read"))],
)
async def get_message(
    event_id: UUID,
    database: DatabaseSessionDependency,
) -> MessageDetailView:
    try:
        return await service.get_message(database, event_id)
    except MessageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/traces/{trace_id}",
    response_model=TraceView,
    dependencies=[Depends(require_permission("audit.read"))],
)
async def get_trace(
    trace_id: UUID,
    database: DatabaseSessionDependency,
) -> TraceView:
    try:
        return await service.get_trace(database, trace_id)
    except TraceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
