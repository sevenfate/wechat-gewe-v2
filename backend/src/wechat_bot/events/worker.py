from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.core.logging import get_logger
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import InboxStatus, NormalizedEvent, WebhookInbox
from wechat_bot.db.session import Database
from wechat_bot.events.dispatcher import EventDispatcher, PluginInvocationRetryableError


class EventDispatcherWorker:
    def __init__(
        self,
        *,
        database: Database,
        dispatcher: EventDispatcher,
        poll_interval_seconds: float = 0.5,
        batch_size: int = 50,
        max_attempts: int = 5,
        lease_seconds: float = 120.0,
        max_retry_delay_seconds: float = 30.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if batch_size <= 0:
            raise ValueError("batch size must be positive")
        if max_attempts <= 0 or lease_seconds <= 0:
            raise ValueError("attempt and lease limits must be positive")
        self._database = database
        self._dispatcher = dispatcher
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._max_retry_delay_seconds = max_retry_delay_seconds
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._logger = get_logger(component="event_dispatcher_worker")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("event dispatcher worker is already running")
        self._stop_requested.clear()
        self._task = asyncio.create_task(self._run(), name="event-dispatcher-worker")

    async def stop(self) -> None:
        self._stop_requested.set()
        task = self._task
        if task is not None:
            await task
        self._task = None

    async def run_once(self) -> int:
        await self._recover_expired_leases()
        event_ids = await self._claim_due_events()
        for event_id in event_ids:
            await self._dispatch_one(event_id)
        return len(event_ids)

    async def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                attempted = await self.run_once()
            except Exception:
                self._logger.exception("event_dispatcher_iteration_failed")
                attempted = 0
            if attempted:
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stop_requested.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _dispatch_one(self, event_id: UUID) -> None:
        async with self._database.session_factory() as session:
            try:
                await self._dispatcher.dispatch(session, event_id)
            except PluginInvocationRetryableError:
                inbox = await self._inbox_for_event(session, event_id)
                if inbox is not None:
                    if inbox.dispatch_attempt_count >= self._max_attempts:
                        inbox.status = InboxStatus.FAILED
                        inbox.error_code = "PLUGIN_DISPATCH_RETRIES_EXHAUSTED"
                        inbox.error_detail = "plugin dispatch retry limit reached"
                    else:
                        delay = self._retry_delay(inbox.dispatch_attempt_count)
                        inbox.status = InboxStatus.NORMALIZED
                        inbox.dispatch_available_at = utc_now() + timedelta(seconds=delay)
                await session.commit()
                self._logger.warning(
                    "event_dispatch_retry_scheduled",
                    event_id=str(event_id),
                    attempt=(inbox.dispatch_attempt_count if inbox is not None else None),
                    exhausted=(inbox is not None and inbox.status is InboxStatus.FAILED),
                )
                return
            except Exception as exc:
                await session.rollback()
                await self._mark_failed(event_id, type(exc).__name__)
                self._logger.exception(
                    "event_dispatch_failed",
                    event_id=str(event_id),
                    error_type=type(exc).__name__,
                )
                return
            await session.commit()

    async def _recover_expired_leases(self) -> None:
        now = utc_now()
        async with self._database.session_factory() as session, session.begin():
            inboxes = list(
                await session.scalars(
                    select(WebhookInbox)
                    .where(
                        WebhookInbox.status == InboxStatus.DISPATCHING,
                        WebhookInbox.dispatch_available_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(self._batch_size)
                )
            )
            for inbox in inboxes:
                inbox.status = InboxStatus.NORMALIZED
                inbox.error_code = "DISPATCH_LEASE_RECOVERED"
                inbox.error_detail = "expired dispatcher lease was recovered"

    async def _claim_due_events(self) -> list[UUID]:
        now = utc_now()
        async with self._database.session_factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(WebhookInbox, NormalizedEvent.id)
                    .join(
                        NormalizedEvent,
                        NormalizedEvent.webhook_inbox_id == WebhookInbox.id,
                    )
                    .where(
                        WebhookInbox.status == InboxStatus.NORMALIZED,
                        WebhookInbox.dispatch_available_at <= now,
                    )
                    .order_by(WebhookInbox.created_at, WebhookInbox.id)
                    .with_for_update(skip_locked=True)
                    .limit(self._batch_size)
                )
            ).all()
            event_ids: list[UUID] = []
            for inbox, event_id in rows:
                inbox.status = InboxStatus.DISPATCHING
                inbox.dispatch_attempt_count += 1
                inbox.dispatch_available_at = now + timedelta(seconds=self._lease_seconds)
                event_ids.append(event_id)
            return event_ids

    def _retry_delay(self, attempt: int) -> float:
        return float(
            min(
                self._poll_interval_seconds * (2 ** min(max(attempt - 1, 0), 10)),
                self._max_retry_delay_seconds,
            )
        )

    @staticmethod
    async def _inbox_for_event(
        session: AsyncSession,
        event_id: UUID,
    ) -> WebhookInbox | None:
        return cast(
            WebhookInbox | None,
            await session.scalar(
                select(WebhookInbox)
                .join(
                    NormalizedEvent,
                    NormalizedEvent.webhook_inbox_id == WebhookInbox.id,
                )
                .where(NormalizedEvent.id == event_id)
            ),
        )

    async def _mark_failed(self, event_id: UUID, error_type: str) -> None:
        async with self._database.session_factory() as session:
            inbox = await session.scalar(
                select(WebhookInbox)
                .join(
                    NormalizedEvent,
                    NormalizedEvent.webhook_inbox_id == WebhookInbox.id,
                )
                .where(NormalizedEvent.id == event_id)
            )
            if inbox is None:
                return
            inbox.status = InboxStatus.FAILED
            inbox.error_code = "EVENT_DISPATCH_FAILED"
            inbox.error_detail = f"dispatcher failed with {error_type}"
            await session.commit()
