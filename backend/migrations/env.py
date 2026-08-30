from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import CheckConstraint, Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.schema import SchemaItem

from wechat_bot.core.config import get_settings
from wechat_bot.db.base import Base
from wechat_bot.db.registry import load_all_models

load_all_models()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url_override = config.attributes.get("database_url")
if database_url_override is not None and not isinstance(database_url_override, str):
    raise TypeError("Alembic database_url override must be a string")
config.set_main_option(
    "sqlalchemy.url",
    database_url_override or get_settings().database_url,
)
target_metadata = Base.metadata
enum_check_constraint_names = frozenset(
    str(constraint.name)
    for table in target_metadata.tables.values()
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint) and constraint._type_bound
)


def include_schema_object(
    object_: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    del object_
    return not (
        type_ == "check_constraint"
        and reflected
        and compare_to is None
        and name in enum_check_constraint_names
    )


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_schema_object,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_schema_object,
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(run_sync_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
