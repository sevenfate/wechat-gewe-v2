from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest

from wechat_bot.core.config import Settings
from wechat_bot.db.base import Base
from wechat_bot.db.models import Workspace
from wechat_bot.db.policy_models import Principal, PrincipalType
from wechat_bot.db.session import Database


@dataclass(frozen=True, slots=True)
class TaskAgentDatabase:
    database: Database
    workspace_id: UUID
    requester_id: UUID
    other_principal_id: UUID


@pytest.fixture
async def task_agent_db(settings: Settings) -> AsyncIterator[TaskAgentDatabase]:
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with database.session_factory() as session, session.begin():
        workspace = Workspace(name="Task Agent Tests", slug="task-agent-tests")
        session.add(workspace)
        await session.flush()
        requester = Principal(
            workspace_id=workspace.id,
            principal_type=PrincipalType.ADMIN_USER,
            external_id="task-agent-requester",
            display_name="Requester",
        )
        other = Principal(
            workspace_id=workspace.id,
            principal_type=PrincipalType.ADMIN_USER,
            external_id="task-agent-other",
            display_name="Other principal",
        )
        session.add_all((requester, other))
        await session.flush()
        seed = TaskAgentDatabase(
            database=database,
            workspace_id=workspace.id,
            requester_id=requester.id,
            other_principal_id=other.id,
        )
    try:
        yield seed
    finally:
        await database.dispose()
