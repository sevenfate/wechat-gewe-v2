from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.policy_models import AclPolicyState


async def lock_authorization_fence(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    shared: bool,
) -> bool:
    """Serialize authorization mutations with the final external send boundary."""

    state = await session.scalar(
        select(AclPolicyState)
        .where(AclPolicyState.workspace_id == workspace_id)
        .with_for_update(read=shared)
    )
    return state is not None
