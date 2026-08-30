from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError
from wechat_bot.core.logging import get_logger
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ConnectionStatus,
    Contact,
    GeweConnection,
    OutboxMessage,
    OutboxStatus,
)
from wechat_bot.db.plugin_models import (
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
)
from wechat_bot.gewe.client import (
    GeWeAPIError,
    GeWeClient,
    GeWeClientError,
    GeWeHTTPError,
    GeWeProtocolError,
    GeWeTransportError,
)
from wechat_bot.gewe.schemas import PostTextRequest, SentTextData
from wechat_bot.outbox.schemas import OutboxAuthorizationContext, TextOutboxPayload
from wechat_bot.outbox.service import TEXT_ACTION_TYPES
from wechat_bot.outbox.state import transition_outbox
from wechat_bot.policy.fence import lock_authorization_fence
from wechat_bot.policy.schemas import AclEvaluationRequest
from wechat_bot.policy.service import (
    InvalidPolicyRuleError,
    PolicyObjectNotFoundError,
    PolicyService,
)


class _TextSenderClient(Protocol):
    async def post_text(self, request: PostTextRequest) -> SentTextData: ...


class _TextSenderClientContext(Protocol):
    async def __aenter__(self) -> _TextSenderClient: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class SenderClientFactory(Protocol):
    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> _TextSenderClientContext: ...


class _Disposition(StrEnum):
    RETRYABLE = "RETRYABLE"
    FINAL = "FINAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SenderOptions:
    poll_interval_seconds: float = 0.25
    max_concurrent_accounts: int = 8
    per_minute_limit: int = 40
    target_interval_seconds: float = 1.0
    group_interval_min_seconds: float = 2.0
    group_interval_max_seconds: float = 5.0
    max_attempts: int = 5
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    retry_jitter_ratio: float = 0.2
    lease_seconds: float = 90.0
    request_timeout_seconds: float = 20.0
    offline_retry_seconds: float = 30.0

    def __post_init__(self) -> None:
        positive_values = {
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_concurrent_accounts": self.max_concurrent_accounts,
            "per_minute_limit": self.per_minute_limit,
            "max_attempts": self.max_attempts,
            "backoff_base_seconds": self.backoff_base_seconds,
            "backoff_max_seconds": self.backoff_max_seconds,
            "lease_seconds": self.lease_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "offline_retry_seconds": self.offline_retry_seconds,
        }
        if any(value <= 0 for value in positive_values.values()):
            raise ValueError("sender limits and durations must be greater than zero")
        if self.target_interval_seconds < 0 or self.group_interval_min_seconds < 0:
            raise ValueError("sender intervals cannot be negative")
        if self.group_interval_max_seconds < self.group_interval_min_seconds:
            raise ValueError("group maximum interval cannot be below its minimum")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff maximum cannot be below its base")
        if not 0 <= self.retry_jitter_ratio < 1:
            raise ValueError("retry jitter ratio must be in [0, 1)")
        minimum_lease = 60 + self.request_timeout_seconds
        if self.lease_seconds < minimum_lease:
            raise ValueError("sender lease must cover rate limiting and request timeout")

    @classmethod
    def from_settings(cls, settings: Settings) -> SenderOptions:
        return cls(
            poll_interval_seconds=settings.sender_poll_interval_seconds,
            max_concurrent_accounts=settings.sender_max_concurrent_accounts,
            per_minute_limit=settings.sender_per_minute_limit,
            target_interval_seconds=settings.sender_target_interval_seconds,
            group_interval_min_seconds=settings.sender_group_interval_min_seconds,
            group_interval_max_seconds=settings.sender_group_interval_max_seconds,
            max_attempts=settings.sender_max_attempts,
            backoff_base_seconds=settings.sender_backoff_base_seconds,
            backoff_max_seconds=settings.sender_backoff_max_seconds,
            retry_jitter_ratio=settings.sender_retry_jitter_ratio,
            lease_seconds=settings.sender_lease_seconds,
            request_timeout_seconds=settings.sender_request_timeout_seconds,
            offline_retry_seconds=settings.sender_offline_retry_seconds,
        )


@dataclass(frozen=True, slots=True)
class _ClaimedMessage:
    id: UUID
    bot_account_id: UUID
    target_wxid: str


@dataclass(frozen=True, slots=True)
class _PreparedSend:
    message_id: UUID
    bot_account_id: UUID
    base_url: str
    token: str = field(repr=False)
    request: PostTextRequest


@dataclass(slots=True)
class SenderWorker:
    session_factory: async_sessionmaker[AsyncSession]
    cipher: CredentialCipher
    policy_service: PolicyService = field(default_factory=PolicyService)
    options: SenderOptions = field(default_factory=SenderOptions)
    client_factory: SenderClientFactory = field(default=cast(SenderClientFactory, GeWeClient))
    clock: Callable[[], datetime] = utc_now
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    random_source: random.Random = field(default_factory=random.Random)
    _stop_event: asyncio.Event = field(init=False, default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = field(init=False, default=None)
    _lifecycle_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _account_locks: dict[UUID, asyncio.Lock] = field(init=False, default_factory=dict)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.running:
                return
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_loop(), name="outbox-sender")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                return
            self._stop_event.set()
        await task
        async with self._lifecycle_lock:
            if self._task is task:
                self._task = None

    async def run_once(self) -> int:
        await self._recover_expired_leases()
        account_ids = await self._due_account_ids()
        if not account_ids:
            return 0

        semaphore = asyncio.Semaphore(self.options.max_concurrent_accounts)

        async def process(account_id: UUID) -> bool:
            async with semaphore:
                lock = self._account_locks.setdefault(account_id, asyncio.Lock())
                async with lock:
                    return await self._process_account(account_id)

        results = await asyncio.gather(*(process(account_id) for account_id in account_ids))
        return sum(results)

    async def _run_loop(self) -> None:
        logger = get_logger(component="outbox_sender")
        while not self._stop_event.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "sender_iteration_failed",
                    error_type=type(exc).__name__,
                )
                processed = 0
            if processed == 0 and not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.options.poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

    async def _process_account(self, account_id: UUID) -> bool:
        claimed = await self._claim_next(account_id)
        if claimed is None:
            return False
        await self._respect_rate_limit(claimed)
        async with self._authorization_guard(claimed.id):
            prepared = await self._prepare_send(claimed.id)
            if prepared is None:
                return True

            try:
                async with self.client_factory(
                    base_url=prepared.base_url,
                    token=prepared.token,
                    timeout_seconds=self.options.request_timeout_seconds,
                ) as client:
                    result = await client.post_text(prepared.request)
            except asyncio.CancelledError:
                await self._finish_unknown(prepared.message_id, "WORKER_CANCELLED_DURING_SEND")
                raise
            except GeWeClientError as exc:
                disposition, error_code = self._classify_gewe_error(exc)
                await self._finish_failure(prepared.message_id, disposition, error_code)
            except Exception:
                await self._finish_unknown(prepared.message_id, "UNEXPECTED_SEND_ERROR")
            else:
                await self._finish_sent(prepared.message_id, result)
        return True

    @asynccontextmanager
    async def _authorization_guard(self, message_id: UUID) -> AsyncIterator[None]:
        async with self.session_factory() as session, session.begin():
            raw_context = await session.scalar(
                select(OutboxMessage.authorization_context).where(OutboxMessage.id == message_id)
            )
            if raw_context is not None:
                try:
                    context = OutboxAuthorizationContext.model_validate(raw_context)
                except ValidationError:
                    context = None
                if context is not None:
                    await lock_authorization_fence(
                        session,
                        context.workspace_id,
                        shared=True,
                    )
            yield

    async def _due_account_ids(self) -> list[UUID]:
        now = self.clock()
        async with self.session_factory() as session:
            result = await session.scalars(
                select(OutboxMessage.bot_account_id)
                .where(
                    OutboxMessage.status.in_((OutboxStatus.PENDING, OutboxStatus.FAILED_RETRYABLE)),
                    OutboxMessage.available_at <= now,
                )
                .group_by(OutboxMessage.bot_account_id)
                .order_by(
                    func.min(OutboxMessage.priority).asc(),
                    func.min(OutboxMessage.created_at).asc(),
                )
                .limit(self.options.max_concurrent_accounts)
            )
            return list(result)

    async def _recover_expired_leases(self) -> None:
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            messages = list(
                await session.scalars(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.status.in_((OutboxStatus.CLAIMED, OutboxStatus.SENDING)),
                        OutboxMessage.available_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(200)
                )
            )
            for message in messages:
                if message.status is OutboxStatus.CLAIMED:
                    transition_outbox(
                        message,
                        OutboxStatus.PENDING,
                        now=now,
                        error_code="CLAIM_LEASE_RECOVERED",
                        available_at=now,
                    )
                else:
                    transition_outbox(
                        message,
                        OutboxStatus.UNKNOWN,
                        now=now,
                        error_code="SENDING_LEASE_EXPIRED",
                    )

    async def _claim_next(self, account_id: UUID) -> _ClaimedMessage | None:
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            locked_account = await session.scalar(
                select(BotAccount.id).where(BotAccount.id == account_id).with_for_update()
            )
            if locked_account is None:
                return None
            in_flight = await session.scalar(
                select(func.count())
                .select_from(OutboxMessage)
                .where(
                    OutboxMessage.bot_account_id == account_id,
                    OutboxMessage.status.in_((OutboxStatus.CLAIMED, OutboxStatus.SENDING)),
                )
            )
            if in_flight:
                return None

            while True:
                message = await session.scalar(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.bot_account_id == account_id,
                        OutboxMessage.status.in_(
                            (OutboxStatus.PENDING, OutboxStatus.FAILED_RETRYABLE)
                        ),
                        OutboxMessage.available_at <= now,
                    )
                    .order_by(
                        OutboxMessage.priority.asc(),
                        OutboxMessage.created_at.asc(),
                        OutboxMessage.id.asc(),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if message is None:
                    return None
                if message.expires_at is not None and _as_utc(message.expires_at) <= now:
                    transition_outbox(
                        message,
                        OutboxStatus.CANCELLED,
                        now=now,
                        error_code="EXPIRED_BEFORE_SEND",
                    )
                    await session.flush()
                    continue

                transition_outbox(
                    message,
                    OutboxStatus.CLAIMED,
                    now=now,
                    available_at=now + timedelta(seconds=self.options.lease_seconds),
                )
                await session.flush()
                return _ClaimedMessage(
                    id=message.id,
                    bot_account_id=message.bot_account_id,
                    target_wxid=message.target_wxid,
                )

    async def _respect_rate_limit(self, claimed: _ClaimedMessage) -> None:
        now = self.clock()
        cutoff = now - timedelta(seconds=60)
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(OutboxMessage.updated_at, OutboxMessage.target_wxid).where(
                        OutboxMessage.bot_account_id == claimed.bot_account_id,
                        OutboxMessage.attempt_count > 0,
                        OutboxMessage.updated_at >= cutoff,
                    )
                )
            ).all()

        attempts = [(_as_utc(row[0]), row[1]) for row in rows]
        delays = [0.0]
        if len(attempts) >= self.options.per_minute_limit:
            recent = sorted((attempted_at for attempted_at, _ in attempts), reverse=True)
            threshold = recent[self.options.per_minute_limit - 1]
            delays.append(60 - (now - threshold).total_seconds())

        target_attempts = [
            attempted_at
            for attempted_at, target_wxid in attempts
            if target_wxid == claimed.target_wxid
        ]
        if target_attempts:
            delays.append(
                self.options.target_interval_seconds - (now - max(target_attempts)).total_seconds()
            )

        if claimed.target_wxid.endswith("@chatroom"):
            group_attempts = [
                attempted_at
                for attempted_at, target_wxid in attempts
                if target_wxid.endswith("@chatroom")
            ]
            if group_attempts:
                group_interval = self.random_source.uniform(
                    self.options.group_interval_min_seconds,
                    self.options.group_interval_max_seconds,
                )
                delays.append(group_interval - (now - max(group_attempts)).total_seconds())

        delay = max(delays)
        if delay <= 0:
            return
        await self._extend_claim_lease(claimed.id, delay)
        await self.sleeper(delay)

    async def _extend_claim_lease(self, message_id: UUID, delay: float) -> None:
        now = self.clock()
        lease_extension = max(self.options.lease_seconds, delay + self.options.lease_seconds)
        async with self.session_factory() as session, session.begin():
            message = await session.get(OutboxMessage, message_id, with_for_update=True)
            if message is not None and message.status is OutboxStatus.CLAIMED:
                message.available_at = now + timedelta(seconds=lease_extension)
                message.updated_at = now

    async def _prepare_send(self, message_id: UUID) -> _PreparedSend | None:
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            row = (
                await session.execute(
                    select(OutboxMessage, BotAccount, GeweConnection)
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
                return None
            message, account, connection = row[0], row[1], row[2]
            if message.status is not OutboxStatus.CLAIMED:
                return None
            if message.expires_at is not None and _as_utc(message.expires_at) <= now:
                transition_outbox(
                    message,
                    OutboxStatus.CANCELLED,
                    now=now,
                    error_code="EXPIRED_BEFORE_SEND",
                )
                return None
            if (
                account.status is BotAccountStatus.DISABLED
                or connection.status is ConnectionStatus.DISABLED
            ):
                transition_outbox(
                    message,
                    OutboxStatus.CANCELLED,
                    now=now,
                    error_code="ACCOUNT_OR_CONNECTION_DISABLED",
                )
                return None
            if message.action_type not in TEXT_ACTION_TYPES:
                transition_outbox(
                    message,
                    OutboxStatus.FAILED_FINAL,
                    now=now,
                    error_code="UNSUPPORTED_ACTION_TYPE",
                )
                return None
            if not await self._authorization_still_valid(session, message):
                transition_outbox(
                    message,
                    OutboxStatus.CANCELLED,
                    now=now,
                    error_code="POLICY_CHANGED",
                )
                return None
            if (
                account.status is not BotAccountStatus.ONLINE
                or connection.status is not ConnectionStatus.ACTIVE
            ):
                transition_outbox(
                    message,
                    OutboxStatus.FAILED_RETRYABLE,
                    now=now,
                    error_code="ACCOUNT_NOT_READY",
                    available_at=now + timedelta(seconds=self.options.offline_retry_seconds),
                )
                return None
            try:
                payload = TextOutboxPayload.model_validate(message.payload)
                token = self.cipher.decrypt(connection.token_ciphertext)
            except ValidationError:
                transition_outbox(
                    message,
                    OutboxStatus.FAILED_FINAL,
                    now=now,
                    error_code="INVALID_OUTBOX_PAYLOAD",
                )
                return None
            except CredentialDecryptionError:
                transition_outbox(
                    message,
                    OutboxStatus.FAILED_FINAL,
                    now=now,
                    error_code="CREDENTIAL_UNAVAILABLE",
                )
                return None

            request = PostTextRequest(
                app_id=account.app_id,
                to_wxid=message.target_wxid,
                content=payload.text,
                ats=",".join(payload.at_wxids) or None,
            )
            transition_outbox(
                message,
                OutboxStatus.SENDING,
                now=now,
                available_at=now + timedelta(seconds=self.options.lease_seconds),
            )
            message.attempt_count += 1
            message.last_attempt_started_at = now
            message.last_attempt_finished_at = None
            await session.flush()
            return _PreparedSend(
                message_id=message.id,
                bot_account_id=message.bot_account_id,
                base_url=connection.api_base_url,
                token=token,
                request=request,
            )

    async def _authorization_still_valid(
        self,
        session: AsyncSession,
        message: OutboxMessage,
    ) -> bool:
        raw_context = message.authorization_context
        if raw_context is None:
            return True
        try:
            context = OutboxAuthorizationContext.model_validate(raw_context)
        except ValidationError:
            return False

        if not await self._target_matches_authorization(session, message, context):
            return False

        row = (
            await session.execute(
                select(PluginDeployment, PluginDeploymentRevision)
                .join(
                    PluginDeploymentRevision,
                    PluginDeploymentRevision.id == PluginDeployment.active_revision_id,
                )
                .where(PluginDeployment.id == context.deployment_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            return False
        deployment, revision = row[0], row[1]
        if (
            deployment.workspace_id != context.workspace_id
            or deployment.status is not PluginDeploymentStatus.RUNNING
            or deployment.active_revision_id != context.deployment_revision_id
            or revision.id != context.deployment_revision_id
            or revision.deployment_id != deployment.id
            or message.action_type not in revision.grants
        ):
            return False

        try:
            decision = await self.policy_service.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.workspace_id,
                    bot_account_id=message.bot_account_id,
                    actor_principal_id=context.actor_principal_id,
                    chatroom_id=context.chatroom_id,
                    contact_id=context.contact_id,
                    resource_type=context.resource_type,
                    resource_id=context.resource_id,
                    parent_plugin_id=context.parent_plugin_id,
                    trace_id=message.trace_id,
                ),
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError):
            return False
        return decision.allowed

    @staticmethod
    async def _target_matches_authorization(
        session: AsyncSession,
        message: OutboxMessage,
        context: OutboxAuthorizationContext,
    ) -> bool:
        if context.chatroom_id is not None:
            target = await session.scalar(
                select(Chatroom.chatroom_id).where(
                    Chatroom.id == context.chatroom_id,
                    Chatroom.bot_account_id == message.bot_account_id,
                )
            )
        elif context.contact_id is not None:
            target = await session.scalar(
                select(Contact.external_id).where(
                    Contact.id == context.contact_id,
                    Contact.bot_account_id == message.bot_account_id,
                    Contact.active.is_(True),
                )
            )
        else:
            return False
        return target == message.target_wxid

    async def _finish_sent(self, message_id: UUID, result: SentTextData) -> None:
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            message = await session.get(OutboxMessage, message_id, with_for_update=True)
            if message is not None and message.status is OutboxStatus.SENDING:
                transition_outbox(message, OutboxStatus.SENT, now=now)
                message.last_attempt_finished_at = now
                message.provider_message_id = result.msg_id
                message.provider_new_message_id = result.new_msg_id
                message.provider_create_time = result.create_time
                message.provider_message_type = result.message_type

    async def _finish_unknown(self, message_id: UUID, error_code: str) -> None:
        now = self.clock()
        async with self.session_factory() as session, session.begin():
            message = await session.get(OutboxMessage, message_id, with_for_update=True)
            if message is not None and message.status is OutboxStatus.SENDING:
                message.last_attempt_finished_at = now
                transition_outbox(
                    message,
                    OutboxStatus.UNKNOWN,
                    now=now,
                    error_code=error_code,
                )

    async def _finish_failure(
        self,
        message_id: UUID,
        disposition: _Disposition,
        error_code: str,
    ) -> None:
        if disposition is _Disposition.UNKNOWN:
            await self._finish_unknown(message_id, error_code)
            return

        now = self.clock()
        async with self.session_factory() as session, session.begin():
            message = await session.get(OutboxMessage, message_id, with_for_update=True)
            if message is None or message.status is not OutboxStatus.SENDING:
                return
            message.last_attempt_finished_at = now
            if (
                disposition is _Disposition.RETRYABLE
                and message.attempt_count < self.options.max_attempts
            ):
                delay = self._backoff_delay(message.attempt_count)
                transition_outbox(
                    message,
                    OutboxStatus.FAILED_RETRYABLE,
                    now=now,
                    error_code=error_code,
                    available_at=now + timedelta(seconds=delay),
                )
                return
            terminal_code = (
                "RETRY_EXHAUSTED" if disposition is _Disposition.RETRYABLE else error_code
            )
            transition_outbox(
                message,
                OutboxStatus.FAILED_FINAL,
                now=now,
                error_code=terminal_code,
            )

    def _backoff_delay(self, attempt_count: int) -> float:
        base_delay = min(
            self.options.backoff_max_seconds,
            self.options.backoff_base_seconds * (2 ** max(attempt_count - 1, 0)),
        )
        jitter = self.random_source.uniform(
            -self.options.retry_jitter_ratio,
            self.options.retry_jitter_ratio,
        )
        return cast(float, max(0.0, base_delay * (1 + jitter)))

    @staticmethod
    def _classify_gewe_error(exc: GeWeClientError) -> tuple[_Disposition, str]:
        if isinstance(exc, GeWeProtocolError):
            return _Disposition.UNKNOWN, "GEWE_RESPONSE_UNKNOWN"
        if isinstance(exc, GeWeTransportError):
            cause = exc.__cause__
            if isinstance(
                cause,
                (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
            ):
                return _Disposition.RETRYABLE, "GEWE_CONNECT_FAILED"
            return _Disposition.UNKNOWN, "GEWE_TRANSPORT_UNKNOWN"
        if isinstance(exc, GeWeHTTPError):
            code = f"GEWE_HTTP_{exc.status_code}"
        elif isinstance(exc, GeWeAPIError):
            code = f"GEWE_API_{exc.ret}"
        else:
            code = "GEWE_SEND_FAILED"
        return (
            (_Disposition.RETRYABLE if exc.retryable else _Disposition.FINAL),
            code,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
