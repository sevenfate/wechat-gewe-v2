from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    BotAccount,
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    WebhookInbox,
)
from wechat_bot.webhooks.normalizer import normalize_gewe_payload


class UnknownWebhookSecretError(LookupError):
    pass


class WebhookDedupConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebhookIngestResult:
    duplicate: bool
    ignored_self: bool


async def ingest_gewe_webhook(
    session: AsyncSession,
    *,
    callback_secret: str,
    raw_body: bytes,
    payload: dict[str, Any],
    received_at: datetime | None = None,
) -> WebhookIngestResult:
    secret_hash = hashlib.sha256(callback_secret.encode("utf-8")).hexdigest()
    connection = await session.scalar(
        select(GeweConnection).where(GeweConnection.callback_secret_hash == secret_hash)
    )
    if connection is None:
        raise UnknownWebhookSecretError

    envelope = normalize_gewe_payload(payload)
    payload_sha256 = hashlib.sha256(raw_body).hexdigest()
    dedup_key = (
        f"message:{envelope.new_msg_id}"
        if envelope.new_msg_id is not None
        else f"payload:{payload_sha256}"
    )
    now = received_at or utc_now()
    connection.last_callback_at = now
    connection.callback_verified_at = connection.callback_verified_at or now
    connection.last_callback_error = None

    existing = await session.scalar(
        select(WebhookInbox).where(
            WebhookInbox.provider == "gewe",
            WebhookInbox.gewe_connection_id == connection.id,
            WebhookInbox.app_id == envelope.app_id,
            WebhookInbox.dedup_key == dedup_key,
        )
    )
    if existing is not None:
        if existing.payload_sha256 != payload_sha256:
            raise WebhookDedupConflictError("webhook dedup key has conflicting payload")
        return WebhookIngestResult(duplicate=True, ignored_self=envelope.is_self)

    trace_id = uuid7()
    inbox = WebhookInbox(
        gewe_connection_id=connection.id,
        app_id=envelope.app_id,
        new_msg_id=envelope.new_msg_id,
        dedup_key=dedup_key,
        payload_sha256=payload_sha256,
        schema_version=envelope.schema_version,
        raw_payload=payload,
        trace_id=trace_id,
        status=InboxStatus.IGNORED_SELF if envelope.is_self else InboxStatus.NORMALIZED,
    )

    try:
        async with session.begin_nested():
            session.add(inbox)
            await session.flush()
    except IntegrityError as exc:
        raced = await session.scalar(
            select(WebhookInbox).where(
                WebhookInbox.provider == "gewe",
                WebhookInbox.gewe_connection_id == connection.id,
                WebhookInbox.app_id == envelope.app_id,
                WebhookInbox.dedup_key == dedup_key,
            )
        )
        if raced is None or raced.payload_sha256 != payload_sha256:
            raise WebhookDedupConflictError("webhook dedup key has conflicting payload") from exc
        return WebhookIngestResult(duplicate=True, ignored_self=envelope.is_self)

    bot_account_id = await session.scalar(
        select(BotAccount.id).where(
            BotAccount.gewe_connection_id == connection.id,
            BotAccount.app_id == envelope.app_id,
        )
    )
    session.add(
        NormalizedEvent(
            webhook_inbox_id=inbox.id,
            bot_account_id=bot_account_id,
            event_type=envelope.event_type,
            conversation_type=envelope.conversation_type,
            conversation_id=envelope.conversation_id,
            actor_wxid=envelope.actor_wxid,
            to_wxid=envelope.to_wxid,
            provider_message_id=envelope.new_msg_id,
            is_self=envelope.is_self,
            occurred_at=envelope.occurred_at,
            content=envelope.content,
            raw_ref=f"db:webhook_inbox/{inbox.id}",
        )
    )
    return WebhookIngestResult(duplicate=False, ignored_self=envelope.is_self)
