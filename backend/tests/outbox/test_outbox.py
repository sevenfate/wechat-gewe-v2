from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import Base
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    ConnectionStatus,
    GeweConnection,
    OutboxMessage,
    OutboxStatus,
    Workspace,
)
from wechat_bot.db.policy_models import AclResourceType
from wechat_bot.db.registry import load_all_models
from wechat_bot.gewe.client import (
    GeWeAPIError,
    GeWeClientError,
    GeWeProtocolError,
    GeWeTransportError,
)
from wechat_bot.gewe.schemas import PostTextRequest, SentTextData
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.outbox.sender import SenderClientFactory, SenderOptions, SenderWorker
from wechat_bot.outbox.service import (
    TEXT_ACTION_TYPE,
    TEXT_REPLY_ACTION_TYPE,
    OutboxIdempotencyConflictError,
    OutboxService,
)

load_all_models()


@dataclass(frozen=True, slots=True)
class OutboxDatabase:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    cipher: CredentialCipher
    workspace_id: UUID


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakeTextClient:
    def __init__(self, factory: FakeClientFactory) -> None:
        self.factory = factory

    async def post_text(self, request: PostTextRequest) -> SentTextData:
        return await self.factory.send(request)


class FakeClientContext:
    def __init__(self, factory: FakeClientFactory) -> None:
        self.client = FakeTextClient(factory)

    async def __aenter__(self) -> FakeTextClient:
        return self.client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback


class FakeClientFactory:
    def __init__(
        self,
        *,
        error_factory: Callable[[], GeWeClientError] | None = None,
        send_delay: float = 0,
    ) -> None:
        self.error_factory = error_factory
        self.send_delay = send_delay
        self.tokens: list[str] = []
        self.base_urls: list[str] = []
        self.timeout_seconds: list[float] = []
        self.requests: list[PostTextRequest] = []
        self.active = 0
        self.max_active = 0

    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> FakeClientContext:
        self.base_urls.append(base_url)
        self.tokens.append(token)
        self.timeout_seconds.append(timeout_seconds)
        return FakeClientContext(self)

    async def send(self, request: PostTextRequest) -> SentTextData:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            if self.error_factory is not None:
                raise self.error_factory()
            return SentTextData.model_validate(
                {
                    "toWxid": request.to_wxid,
                    "createTime": 1_703_841_160,
                    "msgId": 9_007_199_254_740_993,
                    "newMsgId": 9_007_199_254_740_995,
                    "type": 1,
                }
            )
        finally:
            self.active -= 1


@pytest.fixture
async def outbox_db(tmp_path: Path) -> AsyncIterator[OutboxDatabase]:
    database_path = tmp_path / "outbox.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    cipher = CredentialCipher(Fernet.generate_key())
    workspace_id = uuid4()
    async with session_factory() as session, session.begin():
        session.add(Workspace(id=workspace_id, name="Outbox Tests", slug="outbox-tests"))

    yield OutboxDatabase(
        engine=engine,
        session_factory=session_factory,
        cipher=cipher,
        workspace_id=workspace_id,
    )
    await engine.dispose()


async def _create_account(
    database: OutboxDatabase,
    *,
    app_id: str = "wx_app_outbox",
    token: str | None = None,
    account_status: BotAccountStatus = BotAccountStatus.ONLINE,
    connection_status: ConnectionStatus = ConnectionStatus.ACTIVE,
) -> UUID:
    resolved_token = token or "outbox-secret-token"
    connection_id = uuid4()
    account_id = uuid4()
    callback_secret = f"callback-{uuid4().hex}"
    async with database.session_factory() as session, session.begin():
        session.add(
            GeweConnection(
                id=connection_id,
                workspace_id=database.workspace_id,
                name=f"Connection {app_id}",
                api_base_url="https://api.gewe.test",
                token_ciphertext=database.cipher.encrypt(resolved_token),
                token_fingerprint=database.cipher.fingerprint(resolved_token),
                callback_secret_ciphertext=database.cipher.encrypt(callback_secret),
                callback_secret_hash=hashlib.sha256(callback_secret.encode("utf-8")).hexdigest(),
                status=connection_status,
            )
        )
        session.add(
            BotAccount(
                id=account_id,
                gewe_connection_id=connection_id,
                app_id=app_id,
                status=account_status,
            )
        )
    return account_id


async def _enqueue(
    database: OutboxDatabase,
    account_id: UUID,
    *,
    key: str | None = None,
    target_wxid: str = "wxid_target",
    text: str = "hello",
    at_wxids: tuple[str, ...] = (),
    expires_at: datetime | None = None,
) -> UUID:
    async with database.session_factory() as session, session.begin():
        message = await OutboxService().enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key or f"outbox:{uuid4().hex}",
            target_wxid=target_wxid,
            text=text,
            at_wxids=at_wxids,
            expires_at=expires_at,
        )
        return message.id


async def _insert_message(
    database: OutboxDatabase,
    account_id: UUID,
    *,
    status: OutboxStatus,
    available_at: datetime,
    target_wxid: str = "wxid_target",
    attempt_count: int = 0,
    expires_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> UUID:
    message_id = uuid4()
    async with database.session_factory() as session, session.begin():
        session.add(
            OutboxMessage(
                id=message_id,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key=f"outbox:{uuid4().hex}",
                action_type=TEXT_ACTION_TYPE,
                target_wxid=target_wxid,
                payload={"text": "hello", "at_wxids": []},
                payload_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
                status=status,
                priority=100,
                available_at=available_at,
                expires_at=expires_at,
                attempt_count=attempt_count,
                updated_at=updated_at or available_at,
            )
        )
    return message_id


async def _get_message(database: OutboxDatabase, message_id: UUID) -> OutboxMessage:
    async with database.session_factory() as session:
        message = await session.get(OutboxMessage, message_id)
        assert message is not None
        session.expunge(message)
        return message


def _sender_options(**changes: object) -> SenderOptions:
    defaults = SenderOptions(
        poll_interval_seconds=0.01,
        max_concurrent_accounts=8,
        per_minute_limit=100,
        target_interval_seconds=0,
        group_interval_min_seconds=0,
        group_interval_max_seconds=0,
        max_attempts=5,
        backoff_base_seconds=2,
        backoff_max_seconds=8,
        retry_jitter_ratio=0,
        lease_seconds=61,
        request_timeout_seconds=1,
        offline_retry_seconds=1,
    )
    return replace(defaults, **changes)


def _sender(
    database: OutboxDatabase,
    factory: FakeClientFactory,
    clock: FrozenClock,
    *,
    options: SenderOptions | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SenderWorker:
    return SenderWorker(
        session_factory=database.session_factory,
        cipher=database.cipher,
        options=options or _sender_options(),
        client_factory=cast(SenderClientFactory, factory),
        clock=clock,
        sleeper=sleeper,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _transport_error(cause: httpx.RequestError) -> GeWeTransportError:
    error = GeWeTransportError("redacted transport failure", retryable=True)
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_rejects_changed_payload(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    service = OutboxService()
    key = "plugin:test:event:42"
    async with outbox_db.session_factory() as session, session.begin():
        first = await service.enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key,
            target_wxid="wxid_target",
            text="same action",
        )
        repeated = await service.enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key,
            target_wxid="wxid_target",
            text="same action",
        )

        assert repeated.id == first.id
        assert repeated.payload_sha256 == first.payload_sha256
        with pytest.raises(OutboxIdempotencyConflictError):
            await service.enqueue_text(
                session,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key=key,
                target_wxid="wxid_target",
                text="changed action",
            )


@pytest.mark.asyncio
async def test_reply_action_is_restricted_and_participates_in_idempotency_hash(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    service = OutboxService()
    key = "plugin:reply:event:42"
    async with outbox_db.session_factory() as session, session.begin():
        reply = await service.enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key,
            target_wxid="wxid_target",
            text="reply",
            action_type=TEXT_REPLY_ACTION_TYPE,
        )
        assert reply.action_type == TEXT_REPLY_ACTION_TYPE

        with pytest.raises(OutboxIdempotencyConflictError):
            await service.enqueue_text(
                session,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key=key,
                target_wxid="wxid_target",
                text="reply",
                action_type=TEXT_ACTION_TYPE,
            )

        with pytest.raises(ValueError, match="unsupported text action type"):
            await service.enqueue_text(
                session,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key="plugin:reply:invalid",
                target_wxid="wxid_target",
                text="reply",
                action_type="message.send.image",
            )

    factory = FakeClientFactory()
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    assert await _sender(outbox_db, factory, clock).run_once() == 1
    assert factory.requests[0].content == "reply"


@pytest.mark.asyncio
async def test_authorization_context_is_persisted_and_participates_in_hash(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    deployment_id = uuid4()
    first_revision_id = uuid4()
    context = OutboxAuthorizationContext(
        workspace_id=outbox_db.workspace_id,
        deployment_id=deployment_id,
        deployment_revision_id=first_revision_id,
        actor_principal_id=uuid4(),
        chatroom_id=uuid4(),
        resource_type=AclResourceType.COMMAND,
        resource_id="command.builtin.echo.echo",
        parent_plugin_id="builtin.echo",
    )
    service = OutboxService()
    key = "plugin:authorization:event:42"
    async with outbox_db.session_factory() as session, session.begin():
        first = await service.enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key,
            target_wxid="room@chatroom",
            text="authorized reply",
            action_type=TEXT_REPLY_ACTION_TYPE,
            authorization_context=context,
        )
        repeated = await service.enqueue_text(
            session,
            bot_account_id=account_id,
            trace_id=uuid4(),
            idempotency_key=key,
            target_wxid="room@chatroom",
            text="authorized reply",
            action_type=TEXT_REPLY_ACTION_TYPE,
            authorization_context=context,
        )
        assert repeated.id == first.id
        assert first.authorization_context == context.model_dump(mode="json")

        with pytest.raises(OutboxIdempotencyConflictError):
            await service.enqueue_text(
                session,
                bot_account_id=account_id,
                trace_id=uuid4(),
                idempotency_key=key,
                target_wxid="room@chatroom",
                text="authorized reply",
                action_type=TEXT_REPLY_ACTION_TYPE,
                authorization_context=context.model_copy(
                    update={"deployment_revision_id": uuid4()}
                ),
            )


def test_authorization_context_requires_one_bound_conversation(
    outbox_db: OutboxDatabase,
) -> None:
    common = {
        "workspace_id": outbox_db.workspace_id,
        "deployment_id": uuid4(),
        "deployment_revision_id": uuid4(),
        "resource_type": AclResourceType.PLUGIN,
        "resource_id": "builtin.echo",
    }
    with pytest.raises(ValidationError, match="exactly one conversation"):
        OutboxAuthorizationContext(**common)
    with pytest.raises(ValidationError, match="exactly one conversation"):
        OutboxAuthorizationContext(
            **common,
            chatroom_id=uuid4(),
            contact_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_payload_hash_string_ids_and_mentions_reach_gewe_contract(
    outbox_db: OutboxDatabase,
) -> None:
    app_id = "9007199254740993"
    target = "9007199254740995@chatroom"
    account_id = await _create_account(outbox_db, app_id=app_id)
    message_id = await _enqueue(
        outbox_db,
        account_id,
        target_wxid=target,
        text="@群友 你好",
        at_wxids=("wxid_member", "wxid_member"),
    )
    pending = await _get_message(outbox_db, message_id)
    canonical = {
        "actionType": TEXT_ACTION_TYPE,
        "authorizationContext": None,
        "botAccountId": str(account_id),
        "expiresAt": None,
        "payload": {"text": "@群友 你好", "at_wxids": ["wxid_member"]},
        "priority": 100,
        "targetWxid": target,
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert pending.target_wxid == target
    assert pending.payload_sha256 == expected_hash

    factory = FakeClientFactory()
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    assert await _sender(outbox_db, factory, clock).run_once() == 1

    request = factory.requests[0]
    assert request.app_id == app_id
    assert request.to_wxid == target
    assert request.content == "@群友 你好"
    assert request.ats == "wxid_member"
    assert isinstance(request.app_id, str)
    assert isinstance(request.to_wxid, str)


def test_mentions_require_visible_at_text() -> None:
    with pytest.raises(ValidationError, match="visible @ mention"):
        from wechat_bot.outbox.schemas import TextOutboxPayload

        TextOutboxPayload(text="你好", at_wxids=["wxid_member"])


@pytest.mark.asyncio
async def test_success_moves_message_to_sent_and_decrypts_token_only_for_client(
    outbox_db: OutboxDatabase,
) -> None:
    token = "plain-token-that-must-not-be-persisted"
    account_id = await _create_account(outbox_db, token=token)
    message_id = await _enqueue(outbox_db, account_id)
    factory = FakeClientFactory()
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))

    assert await _sender(outbox_db, factory, clock).run_once() == 1

    sent = await _get_message(outbox_db, message_id)
    assert sent.status is OutboxStatus.SENT
    assert sent.attempt_count == 1
    assert sent.last_error_code is None
    assert factory.tokens == [token]
    assert factory.base_urls == ["https://api.gewe.test"]
    assert factory.timeout_seconds == [1]
    assert token not in repr(sent)


@pytest.mark.asyncio
async def test_concurrent_runs_keep_one_account_strictly_serial(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    await _enqueue(outbox_db, account_id, key="same-account:1")
    await _enqueue(outbox_db, account_id, key="same-account:2")
    factory = FakeClientFactory(send_delay=0.03)
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    worker = _sender(outbox_db, factory, clock)

    processed = await asyncio.gather(worker.run_once(), worker.run_once())

    assert processed == [1, 1]
    assert len(factory.requests) == 2
    assert factory.max_active == 1


@pytest.mark.asyncio
async def test_two_accounts_send_concurrently_in_one_iteration(
    outbox_db: OutboxDatabase,
) -> None:
    first_account = await _create_account(outbox_db, app_id="wx_app_first")
    second_account = await _create_account(outbox_db, app_id="wx_app_second")
    await _enqueue(outbox_db, first_account, key="account:first")
    await _enqueue(outbox_db, second_account, key="account:second")
    factory = FakeClientFactory(send_delay=0.25)
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))

    assert await _sender(outbox_db, factory, clock).run_once() == 2
    assert factory.max_active == 2


@pytest.mark.asyncio
async def test_connect_failure_is_retryable_with_exponential_backoff(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    message_id = await _enqueue(outbox_db, account_id)
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    factory = FakeClientFactory(
        error_factory=lambda: _transport_error(httpx.ConnectError("refused"))
    )

    assert await _sender(outbox_db, factory, clock).run_once() == 1

    failed = await _get_message(outbox_db, message_id)
    assert failed.status is OutboxStatus.FAILED_RETRYABLE
    assert failed.last_error_code == "GEWE_CONNECT_FAILED"
    assert failed.attempt_count == 1
    assert _utc(failed.available_at) == clock.current + timedelta(seconds=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_factory", "error_code"),
    [
        (
            lambda: _transport_error(httpx.ReadTimeout("response timed out")),
            "GEWE_TRANSPORT_UNKNOWN",
        ),
        (
            lambda: GeWeProtocolError("invalid response", retryable=False),
            "GEWE_RESPONSE_UNKNOWN",
        ),
    ],
)
async def test_uncertain_result_becomes_unknown_and_is_never_retried(
    outbox_db: OutboxDatabase,
    error_factory: Callable[[], GeWeClientError],
    error_code: str,
) -> None:
    account_id = await _create_account(outbox_db)
    message_id = await _enqueue(outbox_db, account_id)
    factory = FakeClientFactory(error_factory=error_factory)
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    worker = _sender(outbox_db, factory, clock)

    assert await worker.run_once() == 1
    assert await worker.run_once() == 0

    unknown = await _get_message(outbox_db, message_id)
    assert unknown.status is OutboxStatus.UNKNOWN
    assert unknown.last_error_code == error_code
    assert unknown.attempt_count == 1
    assert len(factory.requests) == 1


@pytest.mark.asyncio
async def test_permanent_error_and_retry_exhaustion_end_in_failed_final(
    outbox_db: OutboxDatabase,
) -> None:
    permanent_account = await _create_account(outbox_db, app_id="wx_app_permanent")
    permanent_id = await _enqueue(outbox_db, permanent_account, key="failure:permanent")
    now = datetime.now(UTC) + timedelta(seconds=1)
    permanent_factory = FakeClientFactory(
        error_factory=lambda: GeWeAPIError(400, "invalid target", retryable=False)
    )
    clock = FrozenClock(now)

    assert await _sender(outbox_db, permanent_factory, clock).run_once() == 1
    permanent = await _get_message(outbox_db, permanent_id)
    assert permanent.status is OutboxStatus.FAILED_FINAL
    assert permanent.last_error_code == "GEWE_API_400"

    exhausted_account = await _create_account(outbox_db, app_id="wx_app_exhausted")
    exhausted_id = await _insert_message(
        outbox_db,
        exhausted_account,
        status=OutboxStatus.PENDING,
        available_at=now - timedelta(seconds=1),
        attempt_count=2,
    )
    exhausted_factory = FakeClientFactory(
        error_factory=lambda: _transport_error(httpx.ConnectError("refused"))
    )
    assert (
        await _sender(
            outbox_db,
            exhausted_factory,
            clock,
            options=_sender_options(max_attempts=3),
        ).run_once()
        == 1
    )
    exhausted = await _get_message(outbox_db, exhausted_id)
    assert exhausted.status is OutboxStatus.FAILED_FINAL
    assert exhausted.last_error_code == "RETRY_EXHAUSTED"
    assert exhausted.attempt_count == 3


@pytest.mark.asyncio
async def test_expired_message_is_cancelled_without_calling_gewe(
    outbox_db: OutboxDatabase,
) -> None:
    account_id = await _create_account(outbox_db)
    now = datetime.now(UTC) + timedelta(seconds=1)
    message_id = await _insert_message(
        outbox_db,
        account_id,
        status=OutboxStatus.PENDING,
        available_at=now - timedelta(seconds=2),
        expires_at=now - timedelta(seconds=1),
    )
    factory = FakeClientFactory()

    assert await _sender(outbox_db, factory, FrozenClock(now)).run_once() == 0

    cancelled = await _get_message(outbox_db, message_id)
    assert cancelled.status is OutboxStatus.CANCELLED
    assert cancelled.last_error_code == "EXPIRED_BEFORE_SEND"
    assert factory.requests == []


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_but_stale_send_becomes_unknown(
    outbox_db: OutboxDatabase,
) -> None:
    claimed_account = await _create_account(outbox_db, app_id="wx_app_claimed")
    sending_account = await _create_account(outbox_db, app_id="wx_app_sending")
    now = datetime.now(UTC) + timedelta(seconds=1)
    claimed_id = await _insert_message(
        outbox_db,
        claimed_account,
        status=OutboxStatus.CLAIMED,
        available_at=now - timedelta(seconds=1),
    )
    sending_id = await _insert_message(
        outbox_db,
        sending_account,
        status=OutboxStatus.SENDING,
        available_at=now - timedelta(seconds=1),
        attempt_count=1,
    )
    factory = FakeClientFactory()

    assert await _sender(outbox_db, factory, FrozenClock(now)).run_once() == 1

    recovered = await _get_message(outbox_db, claimed_id)
    uncertain = await _get_message(outbox_db, sending_id)
    assert recovered.status is OutboxStatus.SENT
    assert uncertain.status is OutboxStatus.UNKNOWN
    assert uncertain.last_error_code == "SENDING_LEASE_EXPIRED"
    assert len(factory.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_target", "send_target", "options", "history_age", "expected_delay"),
    [
        (
            "wxid_other",
            "wxid_current",
            _sender_options(per_minute_limit=1),
            10.0,
            50.0,
        ),
        (
            "wxid_same",
            "wxid_same",
            _sender_options(target_interval_seconds=1),
            0.25,
            0.75,
        ),
        (
            "first@chatroom",
            "second@chatroom",
            _sender_options(
                group_interval_min_seconds=3,
                group_interval_max_seconds=3,
            ),
            1.0,
            2.0,
        ),
    ],
)
async def test_database_backed_rate_limits_delay_sends(
    outbox_db: OutboxDatabase,
    history_target: str,
    send_target: str,
    options: SenderOptions,
    history_age: float,
    expected_delay: float,
) -> None:
    account_id = await _create_account(outbox_db)
    now = datetime.now(UTC) + timedelta(seconds=1)
    await _insert_message(
        outbox_db,
        account_id,
        status=OutboxStatus.SENT,
        available_at=now - timedelta(seconds=30),
        target_wxid=history_target,
        attempt_count=1,
        updated_at=now - timedelta(seconds=history_age),
    )
    await _insert_message(
        outbox_db,
        account_id,
        status=OutboxStatus.PENDING,
        available_at=now - timedelta(seconds=1),
        target_wxid=send_target,
    )
    sleeper = RecordingSleeper()
    factory = FakeClientFactory()

    assert (
        await _sender(
            outbox_db,
            factory,
            FrozenClock(now),
            options=options,
            sleeper=sleeper,
        ).run_once()
        == 1
    )

    assert sleeper.delays == [pytest.approx(expected_delay)]
    assert len(factory.requests) == 1


@pytest.mark.asyncio
async def test_worker_start_and_stop_are_idempotent(outbox_db: OutboxDatabase) -> None:
    factory = FakeClientFactory()
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))
    worker = _sender(outbox_db, factory, clock)

    await worker.start()
    await worker.start()
    assert worker.running is True

    await worker.stop()
    await worker.stop()
    assert worker.running is False


@pytest.mark.asyncio
async def test_provider_error_details_do_not_persist_plain_token(
    outbox_db: OutboxDatabase,
) -> None:
    token = "token-must-stay-redacted"
    account_id = await _create_account(outbox_db, token=token)
    message_id = await _enqueue(outbox_db, account_id)
    factory = FakeClientFactory(
        error_factory=lambda: GeWeAPIError(
            500,
            f"upstream leaked {token}",
            retryable=True,
        )
    )
    clock = FrozenClock(datetime.now(UTC) + timedelta(seconds=1))

    await _sender(outbox_db, factory, clock).run_once()

    failed = await _get_message(outbox_db, message_id)
    assert failed.status is OutboxStatus.FAILED_RETRYABLE
    assert token not in (failed.last_error_code or "")
    assert token not in json.dumps(failed.payload)
    assert factory.tokens == [token]
