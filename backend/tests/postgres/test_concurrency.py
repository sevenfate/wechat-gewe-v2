from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, inspect, select

from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import Base
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    ConnectionStatus,
    ConversationType,
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    OutboxMessage,
    OutboxStatus,
    WebhookInbox,
    Workspace,
)
from wechat_bot.db.session import Database
from wechat_bot.events.dispatcher import EventDispatcher
from wechat_bot.events.worker import EventDispatcherWorker
from wechat_bot.gewe.schemas import PostTextRequest, SentTextData
from wechat_bot.outbox.sender import SenderClientFactory, SenderOptions, SenderWorker
from wechat_bot.outbox.service import OutboxService
from wechat_bot.services.webhooks import ingest_gewe_webhook

pytestmark = pytest.mark.postgres


async def _create_account(
    database: Database,
    cipher: CredentialCipher,
    *,
    callback_secret: str | None = None,
) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    connection_id = uuid4()
    account_id = uuid4()
    token = "postgres-integration-token"
    callback_secret = callback_secret or uuid4().hex
    async with database.session_factory() as session, session.begin():
        session.add(Workspace(id=workspace_id, name="PostgreSQL Tests", slug="postgres-tests"))
        await session.flush()
        session.add(
            GeweConnection(
                id=connection_id,
                workspace_id=workspace_id,
                name="Primary",
                api_base_url="https://api.gewe.test",
                token_ciphertext=cipher.encrypt(token),
                token_fingerprint=cipher.fingerprint(token),
                callback_secret_ciphertext=cipher.encrypt(callback_secret),
                callback_secret_hash=hashlib.sha256(callback_secret.encode()).hexdigest(),
                status=ConnectionStatus.ACTIVE,
            )
        )
        await session.flush()
        session.add(
            BotAccount(
                id=account_id,
                gewe_connection_id=connection_id,
                app_id="postgres-app",
                wxid="wxid_postgres_bot",
                status=BotAccountStatus.ONLINE,
            )
        )
    return connection_id, account_id


async def test_migrated_schema_matches_registered_models(postgres_database: Database) -> None:
    async with postgres_database.engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
        )

    assert table_names == set(Base.metadata.tables) | {"alembic_version"}


async def test_concurrent_webhooks_create_one_inbox_and_event(
    postgres_database: Database,
) -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    callback_secret = "postgres-concurrent-callback"
    await _create_account(postgres_database, cipher, callback_secret=callback_secret)
    payload = {
        "appid": "postgres-app",
        "wxid": "wxid_postgres_bot",
        "content": "concurrent hello",
        "createTime": 1_725_000_000,
        "fromUser": "wxid_friend",
        "isSelf": False,
        "msgType": "TEXT",
        "newMsgId": 9_154_000_000_000_000_111,
        "toUser": "wxid_postgres_bot",
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()

    async def ingest() -> bool:
        async with postgres_database.session_factory() as session, session.begin():
            result = await ingest_gewe_webhook(
                session,
                callback_secret=callback_secret,
                raw_body=raw_body,
                payload=payload,
            )
            return result.duplicate

    duplicates = await asyncio.gather(ingest(), ingest())

    async with postgres_database.session_factory() as session:
        inbox_count = await session.scalar(select(func.count()).select_from(WebhookInbox))
        event_count = await session.scalar(select(func.count()).select_from(NormalizedEvent))
    assert sorted(duplicates) == [False, True]
    assert inbox_count == 1
    assert event_count == 1


async def test_event_claim_skips_a_row_locked_by_another_worker(
    postgres_database: Database,
) -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    connection_id, account_id = await _create_account(postgres_database, cipher)
    event_ids: list[UUID] = []
    inbox_ids: list[UUID] = []
    async with postgres_database.session_factory() as session, session.begin():
        for sequence in range(2):
            inbox = WebhookInbox(
                gewe_connection_id=connection_id,
                app_id="postgres-app",
                new_msg_id=str(sequence),
                dedup_key=f"message:{sequence}",
                payload_sha256=f"{sequence + 1:064x}",
                schema_version="v2",
                raw_payload={"sequence": sequence},
                trace_id=uuid4(),
                status=InboxStatus.NORMALIZED,
                created_at=datetime(2026, 1, 1, 0, 0, sequence, tzinfo=UTC),
            )
            session.add(inbox)
            await session.flush()
            event = NormalizedEvent(
                webhook_inbox_id=inbox.id,
                bot_account_id=account_id,
                event_type="gewe.message.text",
                conversation_type=ConversationType.PRIVATE,
                conversation_id="wxid_friend",
                actor_wxid="wxid_friend",
                to_wxid="wxid_postgres_bot",
                provider_message_id=str(sequence),
                content={"text": str(sequence)},
                raw_ref=f"db:webhook_inbox/{inbox.id}",
            )
            session.add(event)
            await session.flush()
            inbox_ids.append(inbox.id)
            event_ids.append(event.id)

    worker = EventDispatcherWorker(
        database=postgres_database,
        dispatcher=cast(EventDispatcher, object()),
        batch_size=1,
    )
    async with postgres_database.session_factory() as blocker, blocker.begin():
        locked_id = await blocker.scalar(
            select(WebhookInbox.id).where(WebhookInbox.id == inbox_ids[0]).with_for_update()
        )
        assert locked_id == inbox_ids[0]
        claimed = await worker._claim_due_events()

    assert claimed == [event_ids[1]]
    async with postgres_database.session_factory() as session:
        statuses = {
            inbox.id: inbox.status
            for inbox in await session.scalars(
                select(WebhookInbox).where(WebhookInbox.id.in_(inbox_ids))
            )
        }
    assert statuses == {
        inbox_ids[0]: InboxStatus.NORMALIZED,
        inbox_ids[1]: InboxStatus.DISPATCHING,
    }


class _BlockingTextClient:
    def __init__(self, factory: _BlockingClientFactory) -> None:
        self._factory = factory

    async def post_text(self, request: PostTextRequest) -> SentTextData:
        self._factory.requests.append(request)
        self._factory.started.set()
        await self._factory.release.wait()
        return SentTextData.model_validate(
            {
                "toWxid": request.to_wxid,
                "createTime": 1_703_841_160,
                "msgId": 9_007_199_254_740_993,
                "newMsgId": 9_007_199_254_740_995,
                "type": 1,
            }
        )


class _BlockingClientContext(AbstractAsyncContextManager[_BlockingTextClient]):
    def __init__(self, factory: _BlockingClientFactory) -> None:
        self._client = _BlockingTextClient(factory)

    async def __aenter__(self) -> _BlockingTextClient:
        return self._client

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


class _BlockingClientFactory:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[PostTextRequest] = []

    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> _BlockingClientContext:
        del base_url, token, timeout_seconds
        return _BlockingClientContext(self)


def _sender_options() -> SenderOptions:
    return SenderOptions(
        poll_interval_seconds=0.01,
        max_concurrent_accounts=1,
        per_minute_limit=100,
        target_interval_seconds=0,
        group_interval_min_seconds=0,
        group_interval_max_seconds=0,
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_max_seconds=2,
        retry_jitter_ratio=0,
        lease_seconds=61,
        request_timeout_seconds=1,
        offline_retry_seconds=1,
    )


async def test_separate_senders_do_not_overlap_one_account(
    postgres_database: Database,
) -> None:
    cipher = CredentialCipher(Fernet.generate_key())
    _, account_id = await _create_account(postgres_database, cipher)
    async with postgres_database.session_factory() as session, session.begin():
        service = OutboxService()
        for sequence in range(2):
            await service.enqueue_text(
                session,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key=f"postgres-outbox:{sequence}",
                target_wxid="wxid_friend",
                text=f"message {sequence}",
            )

    factory = _BlockingClientFactory()
    first_worker = SenderWorker(
        session_factory=postgres_database.session_factory,
        cipher=cipher,
        options=_sender_options(),
        client_factory=cast(SenderClientFactory, factory),
    )
    second_worker = SenderWorker(
        session_factory=postgres_database.session_factory,
        cipher=cipher,
        options=_sender_options(),
        client_factory=cast(SenderClientFactory, factory),
    )

    first_run = asyncio.create_task(first_worker.run_once())
    try:
        await asyncio.wait_for(factory.started.wait(), timeout=5)
        second_result = await asyncio.wait_for(second_worker.run_once(), timeout=5)
    finally:
        factory.release.set()
    first_result = await asyncio.wait_for(first_run, timeout=5)

    async with postgres_database.session_factory() as session:
        statuses = list(
            await session.scalars(
                select(OutboxMessage.status).order_by(OutboxMessage.created_at, OutboxMessage.id)
            )
        )
    assert first_result == 1
    assert second_result == 0
    assert len(factory.requests) == 1
    assert statuses == [OutboxStatus.SENT, OutboxStatus.PENDING]
