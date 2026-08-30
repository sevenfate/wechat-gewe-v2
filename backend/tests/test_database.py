from datetime import UTC

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import inspect, text

from wechat_bot.db.base import Base, utc_now

EXPECTED_CORE_TABLES = {
    "audit_event",
    "bot_account",
    "chatroom",
    "chatroom_membership",
    "contact",
    "gewe_connection",
    "normalized_event",
    "outbox_message",
    "webhook_inbox",
    "workspace",
}


def test_core_models_are_registered() -> None:
    assert EXPECTED_CORE_TABLES <= set(Base.metadata.tables)


def test_utc_now_returns_aware_utc_datetime() -> None:
    now = utc_now()

    assert now.tzinfo is UTC


async def test_test_schema_and_session_are_usable(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    del client  # The client fixture keeps the application lifespan active.
    database = app.state.database

    async with database.engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )

    assert EXPECTED_CORE_TABLES <= table_names

    async for session in database.session():
        assert await session.scalar(text("SELECT 1")) == 1
