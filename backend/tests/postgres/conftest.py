from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from wechat_bot.core.config import Environment, Settings
from wechat_bot.db.base import Base
from wechat_bot.db.registry import load_all_models
from wechat_bot.db.session import Database

BACKEND_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_URL_ENV = "WECHAT_BOT_TEST_POSTGRES_URL"


def _validated_postgres_url() -> str:
    value = os.environ.get(POSTGRES_URL_ENV)
    if not value:
        pytest.skip(f"{POSTGRES_URL_ENV} is not configured")
    url = make_url(value)
    if url.get_backend_name() != "postgresql":
        raise pytest.UsageError(f"{POSTGRES_URL_ENV} must use PostgreSQL")
    if not url.database or not url.database.endswith("_test"):
        raise pytest.UsageError(f"{POSTGRES_URL_ENV} database name must end with _test")
    if url.host not in {"127.0.0.1", "localhost", "postgres"}:
        raise pytest.UsageError(f"{POSTGRES_URL_ENV} must point to a local test service")
    return value


def _reset_public_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def postgres_url() -> str:
    return _validated_postgres_url()


@pytest.fixture(scope="session", autouse=True)
def migrated_postgres(postgres_url: str) -> Iterator[None]:
    load_all_models()
    _reset_public_schema(postgres_url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["database_url"] = postgres_url
    command.upgrade(config, "head")
    command.check(config)
    yield
    _reset_public_schema(postgres_url)


@pytest.fixture(scope="session")
async def postgres_database(
    migrated_postgres: None,
    postgres_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[Database]:
    del migrated_postgres
    settings = Settings(
        environment=Environment.TEST,
        database_url=postgres_url,
        public_base_url="http://testserver",
        local_master_key_path=tmp_path_factory.mktemp("postgres-key") / "master.key",
    )
    database = Database(settings)
    yield database
    await database.dispose()


@pytest.fixture(autouse=True)
async def clean_postgres_tables(postgres_database: Database) -> AsyncIterator[None]:
    table_names = sorted(Base.metadata.tables)
    quoted_tables = ", ".join(f'"{name}"' for name in table_names)
    async with postgres_database.engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {quoted_tables} CASCADE"))
    yield
