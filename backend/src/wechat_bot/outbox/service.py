from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.service import AuthPrincipal
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    AuditEvent,
    BotAccount,
    GeweConnection,
    OutboxMessage,
    OutboxStatus,
)
from wechat_bot.outbox.schemas import OutboxAuthorizationContext, TextOutboxPayload
from wechat_bot.outbox.state import transition_outbox

TEXT_ACTION_TYPE = "message.send.text"
TEXT_REPLY_ACTION_TYPE = "message.reply.text"
TEXT_ACTION_TYPES = frozenset({TEXT_ACTION_TYPE, TEXT_REPLY_ACTION_TYPE})


class OutboxIdempotencyConflictError(ValueError):
    pass


class OutboxAccountNotFoundError(LookupError):
    pass


class OutboxMessageNotFoundError(LookupError):
    pass


class OutboxStateConflictError(ValueError):
    pass


class OutboxService:
    async def list_messages(
        self,
        session: AsyncSession,
        *,
        bot_account_id: UUID | None = None,
        status: OutboxStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[OutboxMessage], int]:
        filters = []
        if bot_account_id is not None:
            filters.append(OutboxMessage.bot_account_id == bot_account_id)
        if status is not None:
            filters.append(OutboxMessage.status == status)
        messages = list(
            await session.scalars(
                select(OutboxMessage)
                .where(*filters)
                .order_by(OutboxMessage.created_at.desc(), OutboxMessage.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(
            select(func.count()).select_from(OutboxMessage).where(*filters)
        )
        return messages, total or 0

    async def get_message(
        self,
        session: AsyncSession,
        message_id: UUID,
    ) -> OutboxMessage:
        message = await session.get(OutboxMessage, message_id)
        if message is None:
            raise OutboxMessageNotFoundError("outbox message not found")
        return message

    async def cancel_message(
        self,
        session: AsyncSession,
        message_id: UUID,
        *,
        actor: AuthPrincipal,
        reason: str,
    ) -> OutboxMessage:
        normalized_reason = self._normalize_manual_reason(reason)
        message, workspace_id = await self._message_for_manual_update(session, message_id)
        cancellable = {
            OutboxStatus.PENDING,
            OutboxStatus.CLAIMED,
            OutboxStatus.FAILED_RETRYABLE,
        }
        if message.status not in cancellable:
            raise OutboxStateConflictError(
                f"outbox message in {message.status.value} cannot be cancelled"
            )
        previous_status = message.status
        now = utc_now()
        transition_outbox(
            message,
            OutboxStatus.CANCELLED,
            now=now,
            error_code="MANUAL_CANCELLED",
        )
        self._record_manual_change(
            session,
            message=message,
            workspace_id=workspace_id,
            actor=actor,
            action="outbox.cancel",
            previous_status=previous_status,
            reason=normalized_reason,
        )
        await session.flush()
        return message

    async def reconcile_unknown(
        self,
        session: AsyncSession,
        message_id: UUID,
        *,
        actor: AuthPrincipal,
        resolution: OutboxStatus,
        reason: str,
    ) -> OutboxMessage:
        if resolution not in {OutboxStatus.SENT, OutboxStatus.FAILED_FINAL}:
            raise ValueError("resolution must be SENT or FAILED_FINAL")
        normalized_reason = self._normalize_manual_reason(reason)
        message, workspace_id = await self._message_for_manual_update(session, message_id)
        if message.status is not OutboxStatus.UNKNOWN:
            raise OutboxStateConflictError(
                f"only UNKNOWN outbox messages can be reconciled, got {message.status.value}"
            )
        previous_status = message.status
        transition_outbox(
            message,
            resolution,
            now=utc_now(),
            error_code=(
                "MANUAL_RECONCILED_SENT"
                if resolution is OutboxStatus.SENT
                else "MANUAL_RECONCILED_FAILED"
            ),
        )
        self._record_manual_change(
            session,
            message=message,
            workspace_id=workspace_id,
            actor=actor,
            action="outbox.reconcile",
            previous_status=previous_status,
            reason=normalized_reason,
        )
        await session.flush()
        return message

    async def enqueue_text(
        self,
        session: AsyncSession,
        *,
        bot_account_id: UUID,
        trace_id: UUID,
        idempotency_key: str,
        target_wxid: str,
        text: str,
        expires_at: datetime | None = None,
        priority: int = 100,
        at_wxids: Sequence[str] = (),
        action_type: str = TEXT_ACTION_TYPE,
        authorization_context: OutboxAuthorizationContext | None = None,
    ) -> OutboxMessage:
        normalized_key = self._normalize_limited_text(
            idempotency_key,
            field_name="idempotency key",
            max_length=255,
        )
        normalized_target = self._normalize_limited_text(
            target_wxid,
            field_name="target wxid",
            max_length=255,
        )
        if not 0 <= priority <= 1_000:
            raise ValueError("priority must be between 0 and 1000")
        if action_type not in TEXT_ACTION_TYPES:
            raise ValueError("unsupported text action type")

        now = utc_now()
        normalized_expiry = self._normalize_expiry(expires_at, now=now)
        payload = TextOutboxPayload(text=text, at_wxids=list(at_wxids))
        payload_json = payload.model_dump(mode="json")
        authorization_json = (
            authorization_context.model_dump(mode="json")
            if authorization_context is not None
            else None
        )
        payload_sha256 = self._action_hash(
            bot_account_id=bot_account_id,
            action_type=action_type,
            target_wxid=normalized_target,
            payload=payload_json,
            authorization_context=authorization_json,
            priority=priority,
            expires_at=normalized_expiry,
        )

        existing = await self._find_by_idempotency_key(session, normalized_key)
        if existing is not None:
            return self._match_existing(existing, payload_sha256)

        account_exists = await session.scalar(
            select(BotAccount.id).where(BotAccount.id == bot_account_id)
        )
        if account_exists is None:
            raise OutboxAccountNotFoundError("bot account not found")

        message = OutboxMessage(
            bot_account_id=bot_account_id,
            trace_id=trace_id,
            idempotency_key=normalized_key,
            action_type=action_type,
            target_wxid=normalized_target,
            payload=payload_json,
            authorization_context=authorization_json,
            payload_sha256=payload_sha256,
            status=OutboxStatus.PENDING,
            priority=priority,
            available_at=now,
            expires_at=normalized_expiry,
            attempt_count=0,
        )
        try:
            async with session.begin_nested():
                session.add(message)
                await session.flush()
        except IntegrityError:
            raced = await self._find_by_idempotency_key(session, normalized_key)
            if raced is None:
                raise
            return self._match_existing(raced, payload_sha256)
        return message

    @staticmethod
    async def _find_by_idempotency_key(
        session: AsyncSession,
        idempotency_key: str,
    ) -> OutboxMessage | None:
        return cast(
            OutboxMessage | None,
            await session.scalar(
                select(OutboxMessage).where(OutboxMessage.idempotency_key == idempotency_key)
            ),
        )

    @staticmethod
    def _match_existing(
        existing: OutboxMessage,
        payload_sha256: str,
    ) -> OutboxMessage:
        if existing.payload_sha256 != payload_sha256:
            raise OutboxIdempotencyConflictError(
                "idempotency key is already bound to a different action"
            )
        return existing

    @staticmethod
    def _normalize_limited_text(value: str, *, field_name: str, max_length: int) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} cannot be blank")
        if len(normalized) > max_length:
            raise ValueError(f"{field_name} cannot exceed {max_length} characters")
        return normalized

    @staticmethod
    def _normalize_expiry(expires_at: datetime | None, *, now: datetime) -> datetime | None:
        if expires_at is None:
            return None
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        normalized = expires_at.astimezone(UTC)
        if normalized <= now:
            raise ValueError("expires_at must be in the future")
        return normalized

    @staticmethod
    def _normalize_manual_reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("manual outbox action reason cannot be blank")
        if len(normalized) > 500:
            raise ValueError("manual outbox action reason cannot exceed 500 characters")
        return normalized

    @staticmethod
    def _action_hash(
        *,
        bot_account_id: UUID,
        action_type: str,
        target_wxid: str,
        payload: dict[str, object],
        authorization_context: dict[str, object] | None,
        priority: int,
        expires_at: datetime | None,
    ) -> str:
        canonical = {
            "actionType": action_type,
            "authorizationContext": authorization_context,
            "botAccountId": str(bot_account_id),
            "expiresAt": expires_at.isoformat() if expires_at is not None else None,
            "payload": payload,
            "priority": priority,
            "targetWxid": target_wxid,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    async def _message_for_manual_update(
        session: AsyncSession,
        message_id: UUID,
    ) -> tuple[OutboxMessage, UUID]:
        row = (
            await session.execute(
                select(OutboxMessage, GeweConnection.workspace_id)
                .join(BotAccount, BotAccount.id == OutboxMessage.bot_account_id)
                .join(
                    GeweConnection,
                    GeweConnection.id == BotAccount.gewe_connection_id,
                )
                .where(OutboxMessage.id == message_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise OutboxMessageNotFoundError("outbox message not found")
        return row[0], row[1]

    @staticmethod
    def _record_manual_change(
        session: AsyncSession,
        *,
        message: OutboxMessage,
        workspace_id: UUID,
        actor: AuthPrincipal,
        action: str,
        previous_status: OutboxStatus,
        reason: str,
    ) -> None:
        session.add(
            AuditEvent(
                workspace_id=workspace_id,
                trace_id=message.trace_id,
                actor_type="ADMIN_USER",
                actor_id=str(actor.user_id),
                action=action,
                object_type="outbox_message",
                object_id=str(message.id),
                result="SUCCESS",
                detail={
                    "from_status": previous_status.value,
                    "to_status": message.status.value,
                    "reason": reason,
                },
            )
        )
