from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from wechat_bot.db.agent_models import (
    AgentEvent,
    AgentEventType,
    AgentInboxKind,
    AgentRun,
    AgentRunStatus,
    AgentSession,
    AgentSessionInbox,
    AgentVersion,
    ImmutableAgentRecordError,
    PendingQuestion,
    PendingQuestionStatus,
)
from wechat_bot.task_agent.schemas import (
    AgentDefinitionCreate,
    AgentRunCreate,
    AgentRunTransition,
    AgentSessionCreate,
    AgentVersionPublish,
    PendingQuestionAnswer,
    PendingQuestionCreate,
)
from wechat_bot.task_agent.service import (
    MAX_PERSISTED_JSON_BYTES,
    ActiveRunConflictError,
    InvalidRunTransitionError,
    PersistedJsonValidationError,
    QuestionAlreadyConsumedError,
    QuestionAnswerForbiddenError,
    QuestionExpiredError,
    RunIdempotencyConflictError,
    TaskAgentService,
)

from .conftest import TaskAgentDatabase


@dataclass(frozen=True, slots=True)
class AgentSeed:
    definition_id: UUID
    version_id: UUID
    session_id: UUID


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def __call__(self) -> datetime:
        return self.current


async def _seed_session(
    fixture: TaskAgentDatabase,
    service: TaskAgentService,
    *,
    suffix: str,
) -> AgentSeed:
    async with fixture.database.session_factory() as session, session.begin():
        definition = await service.create_definition(
            session,
            AgentDefinitionCreate(
                workspace_id=fixture.workspace_id,
                definition_key=f"research-{suffix}",
                name=f"Research Agent {suffix}",
            ),
        )
        version = await service.publish_version(
            session,
            definition.id,
            AgentVersionPublish(
                specification={"model": "test-model", "max_steps": 5},
                published_by_principal_id=fixture.requester_id,
            ),
        )
        agent_session = await service.create_session(
            session,
            AgentSessionCreate(
                workspace_id=fixture.workspace_id,
                agent_version_id=version.id,
                requester_principal_id=fixture.requester_id,
                task_scope={"chatroom_ids": [f"room-{suffix}"]},
            ),
        )
        return AgentSeed(
            definition_id=definition.id,
            version_id=version.id,
            session_id=agent_session.id,
        )


async def test_published_versions_are_additive_and_session_recovers_fixed_context(
    task_agent_db: TaskAgentDatabase,
) -> None:
    service = TaskAgentService()
    seed = await _seed_session(task_agent_db, service, suffix="versions")

    async with task_agent_db.database.session_factory() as session, session.begin():
        second = await service.publish_version(
            session,
            seed.definition_id,
            AgentVersionPublish(
                specification={"model": "new-model", "max_steps": 8},
                published_by_principal_id=task_agent_db.requester_id,
            ),
        )
        assert second.version_number == 2

    async with task_agent_db.database.session_factory() as recovered_session:
        versions = list(
            await recovered_session.scalars(
                select(AgentVersion)
                .where(AgentVersion.definition_id == seed.definition_id)
                .order_by(AgentVersion.version_number)
            )
        )
        state = await service.get_session_state(recovered_session, seed.session_id)

    assert [item.version_number for item in versions] == [1, 2]
    assert versions[0].specification == {"max_steps": 5, "model": "test-model"}
    assert versions[1].specification == {"max_steps": 8, "model": "new-model"}
    assert state.session.agent_version_id == seed.version_id
    assert state.session.requester_principal_id == task_agent_db.requester_id
    assert state.session.task_scope == {"chatroom_ids": ["room-versions"]}
    assert state.session.last_inbox_seq == 0
    assert [item.seq for item in state.events] == [1]
    assert state.events[0].event_type is AgentEventType.SESSION_CREATED

    async with task_agent_db.database.session_factory() as mutation_session:
        stored_version = await mutation_session.get(AgentVersion, seed.version_id)
        assert stored_version is not None
        stored_version.specification = {"model": "mutated"}
        with pytest.raises(ImmutableAgentRecordError):
            await mutation_session.flush()
        await mutation_session.rollback()

        stored_session = await mutation_session.get(AgentSession, seed.session_id)
        assert stored_session is not None
        stored_session.task_scope = {"chatroom_ids": ["mutated"]}
        with pytest.raises(ImmutableAgentRecordError):
            await mutation_session.flush()
        await mutation_session.rollback()

        stored_event = await mutation_session.scalar(
            select(AgentEvent).where(AgentEvent.session_id == seed.session_id)
        )
        assert stored_event is not None
        stored_event.payload = {"mutated": True}
        with pytest.raises(ImmutableAgentRecordError):
            await mutation_session.flush()


async def test_run_idempotency_active_slot_and_sequence_constraints(
    task_agent_db: TaskAgentDatabase,
) -> None:
    service = TaskAgentService()
    seed = await _seed_session(task_agent_db, service, suffix="idempotency")
    request = AgentRunCreate(
        idempotency_key="request-1",
        input_payload={"task": "summarize", "items": [1, 2]},
    )

    async with task_agent_db.database.session_factory() as session, session.begin():
        first = await service.create_run(session, seed.session_id, request)
        duplicate = await service.create_run(session, seed.session_id, request)
        assert duplicate.id == first.id
        with pytest.raises(RunIdempotencyConflictError):
            await service.create_run(
                session,
                seed.session_id,
                AgentRunCreate(
                    idempotency_key="request-1",
                    input_payload={"task": "different"},
                ),
            )
        with pytest.raises(ActiveRunConflictError):
            await service.create_run(
                session,
                seed.session_id,
                AgentRunCreate(
                    idempotency_key="request-2",
                    input_payload={"task": "second"},
                ),
            )

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    AgentRun(
                        session_id=seed.session_id,
                        idempotency_key="database-active-conflict",
                        input_payload={},
                        input_sha256="a" * 64,
                        status=AgentRunStatus.QUEUED,
                        active_slot=1,
                    )
                )
                await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    AgentEvent(
                        session_id=seed.session_id,
                        seq=1,
                        event_type=AgentEventType.SESSION_CREATED,
                        payload={},
                    )
                )
                await session.flush()

    async with task_agent_db.database.session_factory() as recovered_session:
        state = await service.get_session_state(recovered_session, seed.session_id)
    assert state.active_run is not None
    assert state.active_run.id == first.id
    assert [item.seq for item in state.inbox] == [1]
    assert [item.kind for item in state.inbox] == [AgentInboxKind.RUN_REQUEST]
    assert [item.seq for item in state.events] == [1, 2]


async def test_run_state_machine_is_closed_and_terminal_runs_cannot_revive(
    task_agent_db: TaskAgentDatabase,
) -> None:
    service = TaskAgentService()
    seed = await _seed_session(task_agent_db, service, suffix="states")

    async with task_agent_db.database.session_factory() as session, session.begin():
        run = await service.create_run(
            session,
            seed.session_id,
            AgentRunCreate(idempotency_key="state-run", input_payload={"task": "run"}),
        )
        with pytest.raises(InvalidRunTransitionError):
            await service.transition_run(
                session,
                run.id,
                AgentRunTransition(status=AgentRunStatus.WAITING_USER),
            )
        await service.transition_run(
            session,
            run.id,
            AgentRunTransition(status=AgentRunStatus.RUNNING),
        )
        completed = await service.transition_run(
            session,
            run.id,
            AgentRunTransition(status=AgentRunStatus.COMPLETED),
        )
        assert completed.active_slot is None
        assert completed.finished_at is not None
        with pytest.raises(InvalidRunTransitionError):
            await service.transition_run(
                session,
                run.id,
                AgentRunTransition(status=AgentRunStatus.QUEUED),
            )

        replacement = await service.create_run(
            session,
            seed.session_id,
            AgentRunCreate(
                idempotency_key="replacement-run",
                input_payload={"task": "replacement"},
            ),
        )
        await service.transition_run(
            session,
            replacement.id,
            AgentRunTransition(status=AgentRunStatus.PAUSED),
        )
        await service.transition_run(
            session,
            replacement.id,
            AgentRunTransition(status=AgentRunStatus.QUEUED),
        )
        cancelled = await service.transition_run(
            session,
            replacement.id,
            AgentRunTransition(status=AgentRunStatus.CANCELLED),
        )
        assert cancelled.active_slot is None


async def test_question_answer_checks_identity_consumes_once_and_resumes_in_order(
    task_agent_db: TaskAgentDatabase,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 30, 10, 0, tzinfo=UTC))
    service = TaskAgentService(clock=clock)
    seed = await _seed_session(task_agent_db, service, suffix="answers")

    async with task_agent_db.database.session_factory() as session, session.begin():
        run = await service.create_run(
            session,
            seed.session_id,
            AgentRunCreate(idempotency_key="answer-run", input_payload={"task": "ask"}),
        )
        await service.transition_run(
            session,
            run.id,
            AgentRunTransition(status=AgentRunStatus.RUNNING),
        )
        question = await service.ask_question(
            session,
            run.id,
            PendingQuestionCreate(
                allowed_principal_id=task_agent_db.requester_id,
                prompt="Which room should be used?",
                context={"choices": ["a", "b"]},
                expires_at=clock.current + timedelta(minutes=5),
            ),
        )
        question_id = question.id

    async with task_agent_db.database.session_factory() as session, session.begin():
        with pytest.raises(QuestionAnswerForbiddenError):
            await service.answer_question(
                session,
                question_id,
                PendingQuestionAnswer(
                    principal_id=task_agent_db.other_principal_id,
                    answer_payload={"room": "a"},
                ),
            )
        untouched = await session.get(PendingQuestion, question_id)
        assert untouched is not None
        assert untouched.status is PendingQuestionStatus.PENDING
        assert untouched.answered_at is None

        answered = await service.answer_question(
            session,
            question_id,
            PendingQuestionAnswer(
                principal_id=task_agent_db.requester_id,
                answer_payload={"room": "a"},
            ),
        )
        assert answered.run.status is AgentRunStatus.QUEUED
        assert answered.question.status is PendingQuestionStatus.ANSWERED
        assert answered.question.answer_inbox_seq == 2
        assert answered.inbox_item.question_id == question_id
        with pytest.raises(QuestionAlreadyConsumedError):
            await service.answer_question(
                session,
                question_id,
                PendingQuestionAnswer(
                    principal_id=task_agent_db.requester_id,
                    answer_payload={"room": "b"},
                ),
            )

    async with task_agent_db.database.session_factory() as recovered_session:
        state = await service.get_session_state(recovered_session, seed.session_id)
        stored_inbox = list(
            await recovered_session.scalars(
                select(AgentSessionInbox)
                .where(AgentSessionInbox.session_id == seed.session_id)
                .order_by(AgentSessionInbox.seq)
            )
        )
    assert [item.seq for item in state.inbox] == [1, 2]
    assert [item.seq for item in state.events] == list(range(1, 8))
    assert [item.event_type for item in state.events] == [
        AgentEventType.SESSION_CREATED,
        AgentEventType.RUN_CREATED,
        AgentEventType.RUN_STATUS_CHANGED,
        AgentEventType.QUESTION_ASKED,
        AgentEventType.RUN_STATUS_CHANGED,
        AgentEventType.QUESTION_ANSWERED,
        AgentEventType.RUN_STATUS_CHANGED,
    ]
    assert stored_inbox[1].question_id == question_id
    assert state.active_run is not None
    assert state.active_run.status is AgentRunStatus.QUEUED


async def test_expired_answer_and_invalid_json_are_never_consumed(
    task_agent_db: TaskAgentDatabase,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 30, 11, 0, tzinfo=UTC))
    service = TaskAgentService(clock=clock)
    seed = await _seed_session(task_agent_db, service, suffix="expiry")

    async with task_agent_db.database.session_factory() as session, session.begin():
        with pytest.raises(PersistedJsonValidationError):
            await service.create_run(
                session,
                seed.session_id,
                AgentRunCreate(
                    idempotency_key="private-reasoning",
                    input_payload={"chain-of-thought": "must not be persisted"},
                ),
            )
        with pytest.raises(PersistedJsonValidationError):
            await service.create_run(
                session,
                seed.session_id,
                AgentRunCreate(
                    idempotency_key="oversized",
                    input_payload={"text": "x" * MAX_PERSISTED_JSON_BYTES},
                ),
            )
        with pytest.raises(PersistedJsonValidationError):
            await service.create_run(
                session,
                seed.session_id,
                AgentRunCreate(
                    idempotency_key="not-json",
                    input_payload={"value": object()},
                ),
            )

        run = await service.create_run(
            session,
            seed.session_id,
            AgentRunCreate(idempotency_key="expiry-run", input_payload={"task": "ask"}),
        )
        await service.transition_run(
            session,
            run.id,
            AgentRunTransition(status=AgentRunStatus.RUNNING),
        )
        question = await service.ask_question(
            session,
            run.id,
            PendingQuestionCreate(
                allowed_principal_id=task_agent_db.requester_id,
                prompt="This expires soon",
                expires_at=clock.current + timedelta(seconds=1),
            ),
        )

    clock.current += timedelta(seconds=2)
    async with task_agent_db.database.session_factory() as session, session.begin():
        with pytest.raises(QuestionExpiredError):
            await service.answer_question(
                session,
                question.id,
                PendingQuestionAnswer(
                    principal_id=task_agent_db.requester_id,
                    answer_payload={"answer": "late"},
                ),
            )
        stored = await session.get(PendingQuestion, question.id)
        assert stored is not None
        assert stored.status is PendingQuestionStatus.PENDING
        assert stored.answer_payload is None
        assert stored.answer_inbox_seq is None
        expired_run = await service.transition_run(
            session,
            run.id,
            AgentRunTransition(status=AgentRunStatus.EXPIRED),
        )
        assert expired_run.status is AgentRunStatus.EXPIRED
        await session.flush()
        expired_question = await session.get(PendingQuestion, question.id)
        assert expired_question is not None
        assert expired_question.status is PendingQuestionStatus.EXPIRED
        assert expired_question.answer_payload is None

    async with task_agent_db.database.session_factory() as recovered_session:
        state = await service.get_session_state(recovered_session, seed.session_id)
    assert [item.seq for item in state.inbox] == [1]
    assert state.active_run is None
    assert state.questions[0].status is PendingQuestionStatus.EXPIRED


async def test_mutator_refreshes_stale_identity_map_before_transition(
    task_agent_db: TaskAgentDatabase,
) -> None:
    service = TaskAgentService()
    seed = await _seed_session(task_agent_db, service, suffix="stale-identity")
    async with task_agent_db.database.session_factory() as setup_session, setup_session.begin():
        run = await service.create_run(
            setup_session,
            seed.session_id,
            AgentRunCreate(idempotency_key="stale-run", input_payload={"task": "refresh"}),
            workspace_id=task_agent_db.workspace_id,
        )
        run_id = run.id

    async with task_agent_db.database.session_factory() as stale_session:
        stale_run = await stale_session.get(AgentRun, run_id)
        assert stale_run is not None
        assert stale_run.status is AgentRunStatus.QUEUED
        await stale_session.commit()

        async with task_agent_db.database.session_factory() as fresh_session, fresh_session.begin():
            await service.transition_run(
                fresh_session,
                run_id,
                AgentRunTransition(status=AgentRunStatus.RUNNING),
                workspace_id=task_agent_db.workspace_id,
            )

        assert stale_run.status is AgentRunStatus.QUEUED
        refreshed = await service.transition_run(
            stale_session,
            run_id,
            AgentRunTransition(status=AgentRunStatus.WAITING_APPROVAL),
            workspace_id=task_agent_db.workspace_id,
        )
        assert refreshed is stale_run
        assert refreshed.status is AgentRunStatus.WAITING_APPROVAL
        await stale_session.commit()

    async with task_agent_db.database.session_factory() as verification_session:
        stored = await verification_session.get(AgentRun, run_id)
    assert stored is not None
    assert stored.status is AgentRunStatus.WAITING_APPROVAL
