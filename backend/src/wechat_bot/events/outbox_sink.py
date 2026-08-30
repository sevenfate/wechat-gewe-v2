from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.events.dispatcher import (
    InvalidPluginActionError,
    TextActionSubmission,
)
from wechat_bot.outbox.service import (
    OutboxIdempotencyConflictError,
    OutboxService,
)


class OutboxTextActionSink:
    def __init__(self, service: OutboxService | None = None) -> None:
        self._service = service or OutboxService()

    async def submit_text(
        self,
        session: AsyncSession,
        submission: TextActionSubmission,
    ) -> None:
        try:
            await self._service.enqueue_text(
                session,
                bot_account_id=submission.bot_account_id,
                trace_id=submission.trace_id,
                idempotency_key=submission.idempotency_key,
                target_wxid=submission.target_wxid,
                text=submission.text,
                expires_at=submission.expires_at,
                priority=submission.priority,
                action_type="message.reply.text",
                authorization_context=submission.authorization_context,
            )
        except OutboxIdempotencyConflictError as exc:
            raise InvalidPluginActionError("plugin action idempotency conflict") from exc
        except ValueError as exc:
            raise InvalidPluginActionError("plugin reply action is invalid") from exc
