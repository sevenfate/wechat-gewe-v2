from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select

from wechat_bot.auth.constants import TOOL_READ_PERMISSION
from wechat_bot.auth.dependencies import (
    DatabaseSessionDependency,
    require_management_request,
    require_permission,
)
from wechat_bot.db.models import Workspace
from wechat_bot.db.tool_models import ToolCallStatus
from wechat_bot.tool_bridge.schemas import (
    ToolCallListResponse,
    ToolCallView,
    ToolCatalogItem,
    ToolCatalogResponse,
)
from wechat_bot.tool_bridge.service import ToolBrokerService, ToolCallNotFoundError

router = APIRouter(
    prefix="/api/v1/tool-bridge",
    tags=["Tool Bridge"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission(TOOL_READ_PERMISSION)),
    ],
)


@router.get("/catalog", response_model=ToolCatalogResponse)
async def get_tool_catalog(
    request: Request,
    database: DatabaseSessionDependency,
) -> ToolCatalogResponse:
    workspace = await _resolve_workspace(database)
    broker = _broker(request)
    items = await broker.list_catalog(database, workspace_id=workspace.id)
    return ToolCatalogResponse(items=[ToolCatalogItem.model_validate(item) for item in items])


@router.get("/calls", response_model=ToolCallListResponse)
async def list_tool_calls(
    request: Request,
    database: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: ToolCallStatus | None = None,
    tool_name: Annotated[str | None, Query(max_length=160)] = None,
) -> ToolCallListResponse:
    workspace = await _resolve_workspace(database)
    items, total = await _broker(request).list_calls(
        database,
        workspace_id=workspace.id,
        limit=limit,
        offset=offset,
        status=status,
        tool_name=tool_name,
    )
    return ToolCallListResponse(
        items=[ToolCallView.model_validate(item) for item in items],
        total=total,
    )


@router.get("/calls/{call_id}", response_model=ToolCallView)
async def get_tool_call(
    call_id: UUID,
    request: Request,
    database: DatabaseSessionDependency,
) -> ToolCallView:
    workspace = await _resolve_workspace(database)
    try:
        call = await _broker(request).get_call(
            database,
            call_id,
            workspace_id=workspace.id,
        )
    except ToolCallNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="tool call not found",
        ) from exc
    return ToolCallView.model_validate(call)


def _broker(request: Request) -> ToolBrokerService:
    return cast(ToolBrokerService, request.app.state.tool_broker)


async def _resolve_workspace(database: DatabaseSessionDependency) -> Workspace:
    workspaces = list(await database.scalars(select(Workspace).order_by(Workspace.id).limit(2)))
    if not workspaces:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )
    if len(workspaces) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this release supports exactly one workspace",
        )
    return workspaces[0]
