from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.models import (
    AuditEvent,
    ConversationType,
    InboxStatus,
    NormalizedEvent,
    OutboxMessage,
    WebhookInbox,
)
from wechat_bot.db.policy_models import PolicyDecision
from wechat_bot.observability.schemas import (
    MessageDetailView,
    MessageSummaryView,
    TraceAuditEventView,
    TraceOutboxView,
    TracePolicyDecisionView,
    TraceView,
)


class MessageNotFoundError(LookupError):
    pass


class TraceNotFoundError(LookupError):
    pass


class ObservabilityService:
    async def list_messages(
        self,
        session: AsyncSession,
        *,
        bot_account_id: UUID | None = None,
        inbox_status: InboxStatus | None = None,
        conversation_type: ConversationType | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[MessageSummaryView], int]:
        filters = []
        if bot_account_id is not None:
            filters.append(NormalizedEvent.bot_account_id == bot_account_id)
        if inbox_status is not None:
            filters.append(WebhookInbox.status == inbox_status)
        if conversation_type is not None:
            filters.append(NormalizedEvent.conversation_type == conversation_type)
        if conversation_id is not None:
            filters.append(NormalizedEvent.conversation_id == conversation_id)

        base = (
            select(NormalizedEvent, WebhookInbox)
            .join(WebhookInbox, WebhookInbox.id == NormalizedEvent.webhook_inbox_id)
            .where(*filters)
        )
        rows = (
            await session.execute(
                base.order_by(WebhookInbox.created_at.desc(), WebhookInbox.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        total = await session.scalar(
            select(func.count())
            .select_from(NormalizedEvent)
            .join(WebhookInbox, WebhookInbox.id == NormalizedEvent.webhook_inbox_id)
            .where(*filters)
        )
        return [self._summary(event, inbox) for event, inbox in rows], total or 0

    async def get_message(
        self,
        session: AsyncSession,
        event_id: UUID,
    ) -> MessageDetailView:
        row = (
            await session.execute(self._message_query().where(NormalizedEvent.id == event_id))
        ).one_or_none()
        if row is None:
            raise MessageNotFoundError("message not found")
        return self._detail(row[0], row[1])

    async def get_trace(self, session: AsyncSession, trace_id: UUID) -> TraceView:
        message_row = (
            await session.execute(
                self._message_query()
                .where(WebhookInbox.trace_id == trace_id)
                .order_by(WebhookInbox.created_at)
                .limit(1)
            )
        ).one_or_none()
        policy = list(
            await session.scalars(
                select(PolicyDecision)
                .where(PolicyDecision.trace_id == trace_id)
                .order_by(PolicyDecision.created_at, PolicyDecision.id)
            )
        )
        audits = list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.trace_id == trace_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        outbox = list(
            await session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.trace_id == trace_id)
                .order_by(OutboxMessage.created_at, OutboxMessage.id)
            )
        )
        if message_row is None and not policy and not audits and not outbox:
            raise TraceNotFoundError("trace not found")
        return TraceView(
            trace_id=trace_id,
            message=self._detail(message_row[0], message_row[1]) if message_row else None,
            policy_decisions=[
                TracePolicyDecisionView(
                    id=item.id,
                    policy_version=item.policy_version,
                    effect=item.effect,
                    reason=item.reason,
                    request_snapshot=item.request_snapshot,
                    matched_rule_ids=item.matched_rule_ids,
                    created_at=item.created_at,
                )
                for item in policy
            ],
            audit_events=[
                TraceAuditEventView(
                    id=item.id,
                    actor_type=item.actor_type,
                    actor_id=item.actor_id,
                    action=item.action,
                    object_type=item.object_type,
                    object_id=item.object_id,
                    result=item.result,
                    detail=item.detail,
                    created_at=item.created_at,
                )
                for item in audits
            ],
            outbox_messages=[
                TraceOutboxView(
                    id=item.id,
                    bot_account_id=item.bot_account_id,
                    action_type=item.action_type,
                    target_wxid=item.target_wxid,
                    status=item.status,
                    attempt_count=item.attempt_count,
                    last_error_code=item.last_error_code,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in outbox
            ],
        )

    @staticmethod
    def _message_query() -> Select[tuple[NormalizedEvent, WebhookInbox]]:
        return select(NormalizedEvent, WebhookInbox).join(
            WebhookInbox,
            WebhookInbox.id == NormalizedEvent.webhook_inbox_id,
        )

    @classmethod
    def _summary(
        cls,
        event: NormalizedEvent,
        inbox: WebhookInbox,
    ) -> MessageSummaryView:
        raw_text = event.content.get("raw_content", "")
        text = raw_text if isinstance(raw_text, str) else ""
        return MessageSummaryView(
            id=event.id,
            inbox_id=inbox.id,
            trace_id=inbox.trace_id,
            bot_account_id=event.bot_account_id,
            inbox_status=inbox.status,
            event_type=event.event_type,
            conversation_type=event.conversation_type,
            conversation_id=event.conversation_id,
            actor_wxid=event.actor_wxid,
            provider_message_id=event.provider_message_id,
            text_preview=text[:500],
            occurred_at=event.occurred_at,
            received_at=inbox.created_at,
            error_code=inbox.error_code,
        )

    @classmethod
    def _detail(
        cls,
        event: NormalizedEvent,
        inbox: WebhookInbox,
    ) -> MessageDetailView:
        return MessageDetailView(
            **cls._summary(event, inbox).model_dump(),
            content=event.content,
            raw_payload=inbox.raw_payload,
            payload_sha256=inbox.payload_sha256,
            schema_version=inbox.schema_version,
            raw_ref=event.raw_ref,
        )
