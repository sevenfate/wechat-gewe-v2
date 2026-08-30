from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.session import Database


def get_database(request: Request) -> Database:
    database: Database = request.app.state.database
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    database = get_database(request)
    async for session in database.session():
        yield session
