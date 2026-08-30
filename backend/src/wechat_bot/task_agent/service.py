from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.agent_models import (
    AgentDefinition,
    AgentEvent,
    AgentEventType,
    AgentInboxKind,
    AgentRun,
    AgentRunStatus,
    AgentSession,
    AgentSessionInbox,
    AgentVersion,
    PendingQuestion,
    PendingQuestionStatus,
)
from wechat_bot.db.base import utc_now
from wechat_bot.db.models import Workspace
from wechat_bot.db.policy_models import Principal, PrincipalType
from wechat_bot.task_agent.schemas import (
    AgentDefinitionCreate,
    AgentEventView,
    AgentRunCreate,
    AgentRunTransition,
    AgentRunView,
    AgentSessionCreate,
    AgentSessionInboxView,
    AgentSessionStateView,
    AgentSessionView,
    AgentVersionPublish,
    PendingQuestionAnswer,
    PendingQuestionCreate,
    PendingQuestionOverrideAnswer,
    PendingQuestionView,
    QuestionAnswerResult,
    is_private_reasoning_key,
)

MAX_PERSISTED_JSON_BYTES = 64 * 1024
_ACTIVE_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.QUEUED,
        AgentRunStatus.RUNNING,
        AgentRunStatus.WAITING_APPROVAL,
        AgentRunStatus.WAITING_USER,
        AgentRunStatus.PAUSED,
    }
)
_TERMINAL_RUN_STATUSES = frozenset(
    {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
        AgentRunStatus.EXPIRED,
    }
)
_ALLOWED_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.QUEUED: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.PAUSED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.EXPIRED,
        }
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.QUEUED,
            AgentRunStatus.WAITING_APPROVAL,
            AgentRunStatus.WAITING_USER,
            AgentRunStatus.PAUSED,
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.EXPIRED,
        }
    ),
    AgentRunStatus.WAITING_APPROVAL: frozenset(
        {
            AgentRunStatus.QUEUED,
            AgentRunStatus.PAUSED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.EXPIRED,
        }
    ),
    AgentRunStatus.WAITING_USER: frozenset(
        {
            AgentRunStatus.QUEUED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.EXPIRED,
        }
    ),
    AgentRunStatus.PAUSED: frozenset(
        {
            AgentRunStatus.QUEUED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.EXPIRED,
        }
    ),
    AgentRunStatus.COMPLETED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.CANCELLED: frozenset(),
    AgentRunStatus.EXPIRED: frozenset(),
}


class TaskAgentError(RuntimeError):
    pass


class TaskAgentNotFoundError(TaskAgentError, LookupError):
    pass


class TaskAgentConflictError(TaskAgentError):
    pass


class RunIdempotencyConflictError(TaskAgentConflictError):
    pass


class ActiveRunConflictError(TaskAgentConflictError):
    pass


class InvalidRunTransitionError(TaskAgentConflictError):
    pass


class PendingQuestionConflictError(TaskAgentConflictError):
    pass


class QuestionAnswerForbiddenError(TaskAgentError, PermissionError):
    pass


class QuestionExpiredError(TaskAgentConflictError):
    pass


class QuestionAlreadyConsumedError(TaskAgentConflictError):
    pass


class PersistedJsonValidationError(TaskAgentError, ValueError):
    pass


class TaskAgentService:
    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    async def resolve_admin_principal(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        admin_user_id: UUID,
        display_name: str | None,
    ) -> Principal:
        workspace = await self._locked_workspace(session, workspace_id)
        external_id = str(admin_user_id)
        principal = await session.scalar(
            select(Principal)
            .where(
                Principal.workspace_id == workspace.id,
                Principal.principal_type == PrincipalType.ADMIN_USER,
                Principal.external_id == external_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if principal is None:
            principal = Principal(
                workspace_id=workspace.id,
                principal_type=PrincipalType.ADMIN_USER,
                external_id=external_id,
                display_name=display_name,
                active=True,
            )
            session.add(principal)
        else:
            principal.display_name = display_name
            principal.active = True
        await session.flush()
        return principal

    async def create_definition(
        self,
        session: AsyncSession,
        payload: AgentDefinitionCreate,
    ) -> AgentDefinition:
        await self._locked_workspace(session, payload.workspace_id)
        definition = AgentDefinition(
            workspace_id=payload.workspace_id,
            definition_key=payload.definition_key,
            name=payload.name,
            description=payload.description,
        )
        try:
            async with session.begin_nested():
                session.add(definition)
                await session.flush()
        except IntegrityError as exc:
            raise TaskAgentConflictError("agent definition key already exists") from exc
        return definition

    async def list_definitions(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentDefinition], int]:
        filters = (AgentDefinition.workspace_id == workspace_id,)
        definitions = list(
            await session.scalars(
                select(AgentDefinition)
                .where(*filters)
                .order_by(AgentDefinition.created_at.desc(), AgentDefinition.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(
            select(func.count()).select_from(AgentDefinition).where(*filters)
        )
        return definitions, total or 0

    async def get_definition(
        self,
        session: AsyncSession,
        definition_id: UUID,
        *,
        workspace_id: UUID,
    ) -> AgentDefinition:
        definition = await session.scalar(
            select(AgentDefinition).where(
                AgentDefinition.id == definition_id,
                AgentDefinition.workspace_id == workspace_id,
            )
        )
        if definition is None:
            raise TaskAgentNotFoundError("agent definition not found")
        return definition

    async def publish_version(
        self,
        session: AsyncSession,
        definition_id: UUID,
        payload: AgentVersionPublish,
        *,
        workspace_id: UUID | None = None,
    ) -> AgentVersion:
        workspace = await self._locked_workspace(session, workspace_id)
        definition = await session.scalar(
            select(AgentDefinition)
            .where(
                AgentDefinition.id == definition_id,
                AgentDefinition.workspace_id == workspace.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if definition is None:
            raise TaskAgentNotFoundError("agent definition not found")
        if definition.retired_at is not None:
            raise TaskAgentConflictError("retired agent definition cannot be published")
        if payload.published_by_principal_id is not None:
            await self._require_principal(
                session,
                payload.published_by_principal_id,
                definition.workspace_id,
            )
        specification, specification_sha256 = _normalize_json(
            payload.specification,
            field_name="specification",
        )
        current_version = await session.scalar(
            select(func.max(AgentVersion.version_number)).where(
                AgentVersion.definition_id == definition.id
            )
        )
        version = AgentVersion(
            definition_id=definition.id,
            version_number=(current_version or 0) + 1,
            specification=specification,
            specification_sha256=specification_sha256,
            published_by_principal_id=payload.published_by_principal_id,
            published_at=self._now(),
        )
        try:
            async with session.begin_nested():
                session.add(version)
                await session.flush()
        except IntegrityError as exc:
            raise TaskAgentConflictError("agent version publication conflicted") from exc
        return version

    async def list_versions(
        self,
        session: AsyncSession,
        definition_id: UUID,
        *,
        workspace_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentVersion], int]:
        await self.get_definition(session, definition_id, workspace_id=workspace_id)
        filters = (AgentVersion.definition_id == definition_id,)
        versions = list(
            await session.scalars(
                select(AgentVersion)
                .where(*filters)
                .order_by(AgentVersion.version_number.desc(), AgentVersion.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(AgentVersion).where(*filters))
        return versions, total or 0

    async def get_version(
        self,
        session: AsyncSession,
        version_id: UUID,
        *,
        workspace_id: UUID,
    ) -> AgentVersion:
        version = await session.scalar(
            select(AgentVersion)
            .join(AgentDefinition, AgentDefinition.id == AgentVersion.definition_id)
            .where(
                AgentVersion.id == version_id,
                AgentDefinition.workspace_id == workspace_id,
            )
        )
        if version is None:
            raise TaskAgentNotFoundError("agent version not found")
        return version

    async def create_session(
        self,
        session: AsyncSession,
        payload: AgentSessionCreate,
    ) -> AgentSession:
        workspace = await self._locked_workspace(session, payload.workspace_id)
        version_row = (
            await session.execute(
                select(AgentVersion, AgentDefinition)
                .join(AgentDefinition, AgentDefinition.id == AgentVersion.definition_id)
                .where(
                    AgentVersion.id == payload.agent_version_id,
                    AgentDefinition.workspace_id == workspace.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if version_row is None:
            raise TaskAgentNotFoundError("agent version not found in workspace")
        await self._require_principal(
            session,
            payload.requester_principal_id,
            payload.workspace_id,
        )
        task_scope, task_scope_sha256 = _normalize_json(
            payload.task_scope,
            field_name="task_scope",
        )
        agent_session = AgentSession(
            workspace_id=payload.workspace_id,
            agent_version_id=payload.agent_version_id,
            requester_principal_id=payload.requester_principal_id,
            task_scope=task_scope,
            task_scope_sha256=task_scope_sha256,
            last_inbox_seq=0,
            last_event_seq=0,
        )
        session.add(agent_session)
        await session.flush()
        await self._append_event_locked(
            session,
            agent_session,
            event_type=AgentEventType.SESSION_CREATED,
            payload={
                "agent_version_id": str(agent_session.agent_version_id),
                "requester_principal_id": str(agent_session.requester_principal_id),
                "task_scope_sha256": task_scope_sha256,
            },
        )
        return agent_session

    async def list_sessions(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentSession], int]:
        filters = (AgentSession.workspace_id == workspace_id,)
        sessions = list(
            await session.scalars(
                select(AgentSession)
                .where(*filters)
                .order_by(AgentSession.created_at.desc(), AgentSession.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(AgentSession).where(*filters))
        return sessions, total or 0

    async def get_session(
        self,
        session: AsyncSession,
        session_id: UUID,
        *,
        workspace_id: UUID,
    ) -> AgentSession:
        agent_session = await session.scalar(
            select(AgentSession).where(
                AgentSession.id == session_id,
                AgentSession.workspace_id == workspace_id,
            )
        )
        if agent_session is None:
            raise TaskAgentNotFoundError("agent session not found")
        return agent_session

    async def create_run(
        self,
        session: AsyncSession,
        session_id: UUID,
        payload: AgentRunCreate,
        *,
        workspace_id: UUID | None = None,
    ) -> AgentRun:
        input_payload, input_sha256 = _normalize_json(
            payload.input_payload,
            field_name="input_payload",
        )
        agent_session = await self._locked_session(
            session,
            session_id,
            workspace_id=workspace_id,
        )
        existing = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.session_id == session_id,
                AgentRun.idempotency_key == payload.idempotency_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            return self._resolve_idempotent_run(existing, input_payload, input_sha256)
        active = await session.scalar(
            select(AgentRun)
            .where(
                AgentRun.session_id == session_id,
                AgentRun.active_slot == 1,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if active is not None:
            raise ActiveRunConflictError("session already has an active run")

        run = AgentRun(
            session_id=session_id,
            idempotency_key=payload.idempotency_key,
            input_payload=input_payload,
            input_sha256=input_sha256,
            status=AgentRunStatus.QUEUED,
            active_slot=1,
        )
        try:
            async with session.begin_nested():
                session.add(run)
                await session.flush()
        except IntegrityError as exc:
            raced = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.session_id == session_id,
                    AgentRun.idempotency_key == payload.idempotency_key,
                )
                .execution_options(populate_existing=True)
            )
            if raced is not None:
                return self._resolve_idempotent_run(raced, input_payload, input_sha256)
            raise ActiveRunConflictError("session already has an active run") from exc

        inbox = await self._append_inbox_locked(
            session,
            agent_session,
            run_id=run.id,
            actor_principal_id=agent_session.requester_principal_id,
            kind=AgentInboxKind.RUN_REQUEST,
            payload=input_payload,
            payload_sha256=input_sha256,
        )
        await self._append_event_locked(
            session,
            agent_session,
            event_type=AgentEventType.RUN_CREATED,
            run_id=run.id,
            payload={
                "idempotency_key": run.idempotency_key,
                "input_sha256": input_sha256,
                "inbox_seq": inbox.seq,
                "status": run.status.value,
            },
        )
        return run

    async def list_runs(
        self,
        session: AsyncSession,
        session_id: UUID,
        *,
        workspace_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[AgentRun], int]:
        await self.get_session(session, session_id, workspace_id=workspace_id)
        filters = (AgentRun.session_id == session_id,)
        runs = list(
            await session.scalars(
                select(AgentRun)
                .where(*filters)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(AgentRun).where(*filters))
        return runs, total or 0

    async def get_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        workspace_id: UUID,
    ) -> AgentRun:
        run = await session.scalar(
            select(AgentRun)
            .join(AgentSession, AgentSession.id == AgentRun.session_id)
            .where(
                AgentRun.id == run_id,
                AgentSession.workspace_id == workspace_id,
            )
        )
        if run is None:
            raise TaskAgentNotFoundError("agent run not found")
        return run

    async def get_question(
        self,
        session: AsyncSession,
        question_id: UUID,
        *,
        workspace_id: UUID,
    ) -> PendingQuestion:
        question = await session.scalar(
            select(PendingQuestion)
            .join(AgentSession, AgentSession.id == PendingQuestion.session_id)
            .where(
                PendingQuestion.id == question_id,
                AgentSession.workspace_id == workspace_id,
            )
        )
        if question is None:
            raise TaskAgentNotFoundError("pending question not found")
        return question

    async def transition_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        payload: AgentRunTransition,
        *,
        workspace_id: UUID | None = None,
    ) -> AgentRun:
        agent_session, run = await self._locked_run(
            session,
            run_id,
            workspace_id=workspace_id,
        )
        if (
            run.status is AgentRunStatus.WAITING_USER
            and payload.status is AgentRunStatus.QUEUED
            and await self._open_question(session, run.id) is not None
        ):
            raise InvalidRunTransitionError(
                "WAITING_USER can resume only after its pending question is answered"
            )
        await self._transition_locked(
            session,
            agent_session,
            run,
            target=payload.status,
            reason=payload.reason,
            error_code=payload.error_code,
        )
        return run

    async def ask_question(
        self,
        session: AsyncSession,
        run_id: UUID,
        payload: PendingQuestionCreate,
        *,
        workspace_id: UUID | None = None,
    ) -> PendingQuestion:
        agent_session, run = await self._locked_run(
            session,
            run_id,
            workspace_id=workspace_id,
        )
        if run.status is not AgentRunStatus.RUNNING:
            raise InvalidRunTransitionError("questions can only be asked by a RUNNING run")
        await self._require_principal(
            session,
            payload.allowed_principal_id,
            agent_session.workspace_id,
        )
        if self._now() >= _as_utc(payload.expires_at):
            raise QuestionExpiredError("question expiry must be in the future")
        context, _ = _normalize_json(payload.context, field_name="question context")
        if await self._open_question(session, run.id) is not None:
            raise PendingQuestionConflictError("run already has a pending question")

        question = PendingQuestion(
            session_id=agent_session.id,
            run_id=run.id,
            allowed_principal_id=payload.allowed_principal_id,
            prompt=payload.prompt,
            context=context,
            status=PendingQuestionStatus.PENDING,
            open_slot=1,
            expires_at=_as_utc(payload.expires_at),
        )
        try:
            async with session.begin_nested():
                session.add(question)
                await session.flush()
        except IntegrityError as exc:
            raise PendingQuestionConflictError("run already has a pending question") from exc

        await self._append_event_locked(
            session,
            agent_session,
            event_type=AgentEventType.QUESTION_ASKED,
            run_id=run.id,
            question_id=question.id,
            payload={
                "allowed_principal_id": str(question.allowed_principal_id),
                "expires_at": _as_utc(question.expires_at).isoformat(),
            },
        )
        await self._transition_locked(
            session,
            agent_session,
            run,
            target=AgentRunStatus.WAITING_USER,
            reason="pending user answer",
        )
        return question

    async def answer_question(
        self,
        session: AsyncSession,
        question_id: UUID,
        payload: PendingQuestionAnswer,
        *,
        workspace_id: UUID | None = None,
    ) -> QuestionAnswerResult:
        agent_session, run, question = await self._locked_question(
            session,
            question_id,
            workspace_id=workspace_id,
        )
        self._validate_answerable_question(question, run)
        if payload.principal_id != question.allowed_principal_id:
            raise QuestionAnswerForbiddenError("principal is not allowed to answer this question")
        await self._require_principal(
            session,
            payload.principal_id,
            agent_session.workspace_id,
        )
        return await self._consume_question_answer(
            session,
            agent_session,
            run,
            question,
            actor_principal_id=payload.principal_id,
            answer_payload=payload.answer_payload,
            answer_mode="RUNTIME_PRINCIPAL",
            transition_reason="user answer received",
        )

    async def override_answer_question(
        self,
        session: AsyncSession,
        question_id: UUID,
        payload: PendingQuestionOverrideAnswer,
        *,
        actor_principal_id: UUID,
        workspace_id: UUID | None = None,
    ) -> QuestionAnswerResult:
        agent_session, run, question = await self._locked_question(
            session,
            question_id,
            workspace_id=workspace_id,
        )
        self._validate_answerable_question(question, run)
        await self._require_principal(
            session,
            actor_principal_id,
            agent_session.workspace_id,
        )
        return await self._consume_question_answer(
            session,
            agent_session,
            run,
            question,
            actor_principal_id=actor_principal_id,
            answer_payload=payload.answer_payload,
            answer_mode="ADMIN_OVERRIDE",
            answer_reason=payload.reason,
            transition_reason="administrator override answer received",
        )

    async def _consume_question_answer(
        self,
        session: AsyncSession,
        agent_session: AgentSession,
        run: AgentRun,
        question: PendingQuestion,
        *,
        actor_principal_id: UUID,
        answer_payload: dict[str, Any],
        answer_mode: str,
        transition_reason: str,
        answer_reason: str | None = None,
    ) -> QuestionAnswerResult:
        answer_payload, answer_sha256 = _normalize_json(
            answer_payload,
            field_name="answer_payload",
        )
        inbox = await self._append_inbox_locked(
            session,
            agent_session,
            run_id=run.id,
            actor_principal_id=actor_principal_id,
            kind=AgentInboxKind.QUESTION_ANSWER,
            question_id=question.id,
            payload=answer_payload,
            payload_sha256=answer_sha256,
        )
        now = self._now()
        question.status = PendingQuestionStatus.ANSWERED
        question.open_slot = None
        question.answered_at = now
        question.answered_by_principal_id = actor_principal_id
        question.answer_payload = answer_payload
        question.answer_sha256 = answer_sha256
        question.answer_inbox_seq = inbox.seq
        question.closed_at = now
        await self._append_event_locked(
            session,
            agent_session,
            event_type=AgentEventType.QUESTION_ANSWERED,
            run_id=run.id,
            question_id=question.id,
            payload={
                "answer_sha256": answer_sha256,
                "inbox_seq": inbox.seq,
                "answer_mode": answer_mode,
                "actor_principal_id": str(actor_principal_id),
                "reason": answer_reason,
            },
        )
        await self._transition_locked(
            session,
            agent_session,
            run,
            target=AgentRunStatus.QUEUED,
            reason=transition_reason,
        )
        await session.flush()
        return QuestionAnswerResult(
            question=PendingQuestionView.model_validate(question),
            inbox_item=AgentSessionInboxView.model_validate(inbox),
            run=AgentRunView.model_validate(run),
        )

    async def get_session_state(
        self,
        session: AsyncSession,
        session_id: UUID,
        *,
        workspace_id: UUID | None = None,
        history_limit: int = 100,
    ) -> AgentSessionStateView:
        if not 1 <= history_limit <= 200:
            raise ValueError("history_limit must be between 1 and 200")
        resolved_workspace = await self._workspace_for_read(session, workspace_id)
        agent_session = await self.get_session(
            session,
            session_id,
            workspace_id=resolved_workspace.id,
        )
        active_run = await session.scalar(
            select(AgentRun).where(
                AgentRun.session_id == session_id,
                AgentRun.active_slot == 1,
            )
        )
        inbox = list(
            await session.scalars(
                select(AgentSessionInbox)
                .where(AgentSessionInbox.session_id == session_id)
                .order_by(AgentSessionInbox.seq.desc())
                .limit(history_limit + 1)
            )
        )
        events = list(
            await session.scalars(
                select(AgentEvent)
                .where(AgentEvent.session_id == session_id)
                .order_by(AgentEvent.seq.desc())
                .limit(history_limit + 1)
            )
        )
        questions = list(
            await session.scalars(
                select(PendingQuestion)
                .where(PendingQuestion.session_id == session_id)
                .order_by(PendingQuestion.created_at.desc(), PendingQuestion.id.desc())
                .limit(history_limit + 1)
            )
        )
        inbox_has_more = len(inbox) > history_limit
        events_has_more = len(events) > history_limit
        questions_has_more = len(questions) > history_limit
        inbox = list(reversed(inbox[:history_limit]))
        events = list(reversed(events[:history_limit]))
        questions = list(reversed(questions[:history_limit]))
        return AgentSessionStateView(
            session=AgentSessionView.model_validate(agent_session),
            active_run=(AgentRunView.model_validate(active_run) if active_run else None),
            inbox=[AgentSessionInboxView.model_validate(item) for item in inbox],
            events=[AgentEventView.model_validate(item) for item in events],
            questions=[PendingQuestionView.model_validate(item) for item in questions],
            inbox_has_more=inbox_has_more,
            events_has_more=events_has_more,
            questions_has_more=questions_has_more,
        )

    async def _locked_session(
        self,
        session: AsyncSession,
        session_id: UUID,
        *,
        workspace_id: UUID | None,
    ) -> AgentSession:
        workspace = await self._locked_workspace(session, workspace_id)
        agent_session = await session.scalar(
            select(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.workspace_id == workspace.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if agent_session is None:
            raise TaskAgentNotFoundError("agent session not found")
        return agent_session

    async def _locked_run(
        self,
        session: AsyncSession,
        run_id: UUID,
        *,
        workspace_id: UUID | None,
    ) -> tuple[AgentSession, AgentRun]:
        workspace = await self._locked_workspace(session, workspace_id)
        row = (
            await session.execute(
                select(AgentSession, AgentRun)
                .join(AgentRun, AgentRun.session_id == AgentSession.id)
                .where(
                    AgentRun.id == run_id,
                    AgentSession.workspace_id == workspace.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise TaskAgentNotFoundError("agent run not found")
        return row[0], row[1]

    async def _locked_question(
        self,
        session: AsyncSession,
        question_id: UUID,
        *,
        workspace_id: UUID | None,
    ) -> tuple[AgentSession, AgentRun, PendingQuestion]:
        workspace = await self._locked_workspace(session, workspace_id)
        row = (
            await session.execute(
                select(AgentSession, AgentRun, PendingQuestion)
                .join(AgentRun, AgentRun.session_id == AgentSession.id)
                .join(PendingQuestion, PendingQuestion.run_id == AgentRun.id)
                .where(
                    PendingQuestion.id == question_id,
                    PendingQuestion.session_id == AgentSession.id,
                    AgentSession.workspace_id == workspace.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if row is None:
            raise TaskAgentNotFoundError("pending question not found")
        return row[0], row[1], row[2]

    async def _locked_workspace(
        self,
        session: AsyncSession,
        workspace_id: UUID | None,
    ) -> Workspace:
        statement = select(Workspace).order_by(Workspace.id).limit(2)
        if workspace_id is not None:
            statement = statement.where(Workspace.id == workspace_id)
        workspaces = list(
            await session.scalars(
                statement.with_for_update().execution_options(populate_existing=True)
            )
        )
        if not workspaces:
            raise TaskAgentNotFoundError("workspace not found")
        if len(workspaces) > 1:
            raise TaskAgentConflictError("this release supports exactly one workspace")
        return workspaces[0]

    async def _workspace_for_read(
        self,
        session: AsyncSession,
        workspace_id: UUID | None,
    ) -> Workspace:
        statement = select(Workspace).order_by(Workspace.id).limit(2)
        if workspace_id is not None:
            statement = statement.where(Workspace.id == workspace_id)
        workspaces = list(await session.scalars(statement))
        if not workspaces:
            raise TaskAgentNotFoundError("workspace not found")
        if len(workspaces) > 1:
            raise TaskAgentConflictError("this release supports exactly one workspace")
        return workspaces[0]

    def _validate_answerable_question(
        self,
        question: PendingQuestion,
        run: AgentRun,
    ) -> None:
        if question.status is not PendingQuestionStatus.PENDING:
            if question.status is PendingQuestionStatus.EXPIRED:
                raise QuestionExpiredError("pending question has expired")
            raise QuestionAlreadyConsumedError("pending question has already been consumed")
        if self._now() >= _as_utc(question.expires_at):
            raise QuestionExpiredError("pending question has expired")
        if run.status is not AgentRunStatus.WAITING_USER or run.active_slot != 1:
            raise PendingQuestionConflictError("run is not waiting for a user answer")

    async def _transition_locked(
        self,
        session: AsyncSession,
        agent_session: AgentSession,
        run: AgentRun,
        *,
        target: AgentRunStatus,
        reason: str | None,
        error_code: str | None = None,
    ) -> None:
        source = run.status
        if target not in _ALLOWED_TRANSITIONS[source]:
            raise InvalidRunTransitionError(
                f"run cannot transition from {source.value} to {target.value}"
            )
        now = self._now()
        run.status = target
        run.last_error_code = error_code
        if target is AgentRunStatus.RUNNING and run.started_at is None:
            run.started_at = now
        if target in _TERMINAL_RUN_STATUSES:
            run.active_slot = None
            run.finished_at = now
            await self._close_open_questions(
                session,
                run.id,
                expired=target is AgentRunStatus.EXPIRED,
                now=now,
            )
        elif target in _ACTIVE_RUN_STATUSES:
            run.active_slot = 1
            run.finished_at = None
        await self._append_event_locked(
            session,
            agent_session,
            event_type=AgentEventType.RUN_STATUS_CHANGED,
            run_id=run.id,
            payload={
                "from": source.value,
                "to": target.value,
                "reason": reason,
                "error_code": error_code,
            },
        )
        await session.flush()

    async def _append_inbox_locked(
        self,
        session: AsyncSession,
        agent_session: AgentSession,
        *,
        run_id: UUID,
        actor_principal_id: UUID,
        kind: AgentInboxKind,
        payload: dict[str, Any],
        payload_sha256: str,
        question_id: UUID | None = None,
    ) -> AgentSessionInbox:
        agent_session.last_inbox_seq += 1
        item = AgentSessionInbox(
            session_id=agent_session.id,
            seq=agent_session.last_inbox_seq,
            kind=kind,
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            question_id=question_id,
            payload=payload,
            payload_sha256=payload_sha256,
        )
        session.add(item)
        await session.flush()
        return item

    async def _append_event_locked(
        self,
        session: AsyncSession,
        agent_session: AgentSession,
        *,
        event_type: AgentEventType,
        payload: dict[str, Any],
        run_id: UUID | None = None,
        question_id: UUID | None = None,
    ) -> AgentEvent:
        normalized, _ = _normalize_json(payload, field_name="event payload")
        agent_session.last_event_seq += 1
        event = AgentEvent(
            session_id=agent_session.id,
            seq=agent_session.last_event_seq,
            run_id=run_id,
            question_id=question_id,
            event_type=event_type,
            payload=normalized,
        )
        session.add(event)
        await session.flush()
        return event

    async def _require_principal(
        self,
        session: AsyncSession,
        principal_id: UUID,
        workspace_id: UUID,
    ) -> Principal:
        principal = await session.scalar(
            select(Principal)
            .where(
                Principal.id == principal_id,
                Principal.workspace_id == workspace_id,
                Principal.active.is_(True),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if principal is None:
            raise TaskAgentNotFoundError("active principal not found in workspace")
        return principal

    @staticmethod
    async def _open_question(
        session: AsyncSession,
        run_id: UUID,
    ) -> PendingQuestion | None:
        return cast(
            PendingQuestion | None,
            await session.scalar(
                select(PendingQuestion)
                .where(
                    PendingQuestion.run_id == run_id,
                    PendingQuestion.open_slot == 1,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ),
        )

    @staticmethod
    async def _close_open_questions(
        session: AsyncSession,
        run_id: UUID,
        *,
        expired: bool,
        now: datetime,
    ) -> None:
        questions = list(
            await session.scalars(
                select(PendingQuestion)
                .where(
                    PendingQuestion.run_id == run_id,
                    PendingQuestion.open_slot == 1,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        for question in questions:
            question.status = (
                PendingQuestionStatus.EXPIRED if expired else PendingQuestionStatus.CANCELLED
            )
            question.open_slot = None
            question.closed_at = now

    @staticmethod
    def _resolve_idempotent_run(
        existing: AgentRun,
        input_payload: dict[str, Any],
        input_sha256: str,
    ) -> AgentRun:
        if existing.input_sha256 != input_sha256 or existing.input_payload != input_payload:
            raise RunIdempotencyConflictError("idempotency key is already bound to different input")
        return existing

    def _now(self) -> datetime:
        return _as_utc(self._clock())


def _normalize_json(
    value: dict[str, Any],
    *,
    field_name: str,
) -> tuple[dict[str, Any], str]:
    _reject_private_reasoning(value, path=field_name)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise PersistedJsonValidationError(f"{field_name} must be JSON serializable") from exc
    if len(raw) > MAX_PERSISTED_JSON_BYTES:
        raise PersistedJsonValidationError(f"{field_name} exceeds {MAX_PERSISTED_JSON_BYTES} bytes")
    normalized = json.loads(raw)
    if not isinstance(normalized, dict):
        raise PersistedJsonValidationError(f"{field_name} must be a JSON object")
    return cast(dict[str, Any], normalized), hashlib.sha256(raw).hexdigest()


def _reject_private_reasoning(value: Any, *, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PersistedJsonValidationError(f"{path} keys must be strings")
            if is_private_reasoning_key(key):
                raise PersistedJsonValidationError(
                    f"{path} cannot persist private reasoning fields"
                )
            _reject_private_reasoning(item, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_private_reasoning(item, path=f"{path}[{index}]")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
