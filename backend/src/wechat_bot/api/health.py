from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from wechat_bot.api.dependencies import get_database
from wechat_bot.core.config import Environment, Settings
from wechat_bot.db.session import Database

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> dict[str, str]:
    try:
        await database.ping()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc
    settings: Settings = request.app.state.settings
    if settings.environment is not Environment.TEST:
        sender_running = request.app.state.sender_worker.running
        dispatcher_running = request.app.state.event_dispatcher_worker.running
        if not sender_running or not dispatcher_running:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="background workers unavailable",
            )
    return {"status": "ready"}
