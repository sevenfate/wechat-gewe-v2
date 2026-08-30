from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from wechat_bot.auth.constants import (
    AGENT_QUESTION_OVERRIDE_PERMISSION,
    AGENT_READ_PERMISSION,
    AGENT_RUN_PERMISSION,
    AGENT_WRITE_PERMISSION,
    CSRF_HEADER_NAME,
)
from wechat_bot.db.agent_models import (
    AgentEvent,
    AgentEventType,
    AgentSession,
    AgentSessionInbox,
    AgentVersion,
    PendingQuestion,
)
from wechat_bot.db.models import AuditEvent, Workspace
from wechat_bot.db.policy_models import Principal, PrincipalType

OPERATOR_PASSWORD = "task agent operator password"


@dataclass(frozen=True, slots=True)
class AgentApiSeed:
    workspace_id: UUID
    allowed_principal_id: UUID
    other_principal_id: UUID


async def _seed_workspace(app: FastAPI, *, suffix: str = "default") -> AgentApiSeed:
    async with app.state.database.session_factory() as database, database.begin():
        workspace = Workspace(name=f"Agent API {suffix}", slug=f"agent-api-{suffix}")
        database.add(workspace)
        await database.flush()
        allowed = Principal(
            workspace_id=workspace.id,
            principal_type=PrincipalType.GROUP_MEMBER,
            external_id=f"allowed-member-{suffix}",
            display_name="Allowed member",
        )
        other = Principal(
            workspace_id=workspace.id,
            principal_type=PrincipalType.GROUP_MEMBER,
            external_id=f"other-member-{suffix}",
            display_name="Other member",
        )
        database.add_all([allowed, other])
        await database.flush()
        return AgentApiSeed(
            workspace_id=workspace.id,
            allowed_principal_id=allowed.id,
            other_principal_id=other.id,
        )


async def _create_operator(
    client: AsyncClient,
    *,
    suffix: str,
    permission_codes: list[str],
) -> tuple[str, UUID]:
    username = f"agent-{suffix}"
    role = await client.post(
        "/api/v1/admin/roles",
        json={"code": username, "name": f"Agent {suffix}"},
    )
    assert role.status_code == 201
    binding = await client.put(
        f"/api/v1/admin/roles/{role.json()['id']}/permissions",
        json={"permission_codes": permission_codes},
    )
    assert binding.status_code == 200
    user = await client.post(
        "/api/v1/admin/users",
        json={
            "username": username,
            "display_name": f"Agent {suffix}",
            "password": OPERATOR_PASSWORD,
        },
    )
    assert user.status_code == 201
    user_binding = await client.put(
        f"/api/v1/admin/users/{user.json()['id']}/roles",
        json={"role_codes": [username]},
    )
    assert user_binding.status_code == 200
    return username, UUID(user.json()["id"])


async def _login_operator(client: AsyncClient, username: str) -> None:
    assert (await client.post("/api/auth/logout")).status_code == 200
    csrf = await client.get("/api/auth/csrf")
    login = await client.post(
        "/api/auth/login",
        headers={CSRF_HEADER_NAME: csrf.json()["csrf_token"]},
        json={"username": username, "password": OPERATOR_PASSWORD},
    )
    assert login.status_code == 200
    client.headers[CSRF_HEADER_NAME] = login.json()["csrf_token"]


async def _create_definition_and_version(
    client: AsyncClient,
    seed: AgentApiSeed,
    *,
    suffix: str,
) -> tuple[dict[str, object], dict[str, object]]:
    definition_response = await client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(seed.workspace_id),
            "definition_key": f"assistant-{suffix}",
            "name": f"Assistant {suffix}",
            "description": "Task agent API test definition",
        },
    )
    assert definition_response.status_code == 201
    definition = definition_response.json()
    version_response = await client.post(
        f"/api/v1/task-agent/definitions/{definition['id']}/versions",
        json={"specification": {"model": "test-model", "tools": ["echo"]}},
    )
    assert version_response.status_code == 201
    return definition, version_response.json()


async def _create_waiting_question(
    client: AsyncClient,
    seed: AgentApiSeed,
    version_id: object,
    *,
    suffix: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    session_response = await client.post(
        "/api/v1/task-agent/sessions",
        json={
            "workspace_id": str(seed.workspace_id),
            "agent_version_id": version_id,
            "task_scope": {"chatroom_ids": [f"room-{suffix}"]},
        },
    )
    assert session_response.status_code == 201
    agent_session = session_response.json()
    run_response = await client.post(
        f"/api/v1/task-agent/sessions/{agent_session['id']}/runs",
        json={"idempotency_key": suffix, "input_payload": {"task": "ask"}},
    )
    assert run_response.status_code == 201
    run = run_response.json()
    transition = await client.post(
        f"/api/v1/task-agent/runs/{run['id']}/transition",
        json={"status": "RUNNING"},
    )
    assert transition.status_code == 200
    question_response = await client.post(
        f"/api/v1/task-agent/runs/{run['id']}/questions",
        json={
            "allowed_principal_id": str(seed.allowed_principal_id),
            "prompt": "Continue?",
            "context": {"choices": ["yes", "no"]},
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
    )
    assert question_response.status_code == 201
    return agent_session, run, question_response.json()


async def test_task_agent_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/task-agent/context")).status_code == 401
    assert (await client.get("/api/v1/task-agent/definitions")).status_code == 401


async def test_management_identity_override_history_and_audit_workflow(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="workflow")
    me = await admin_client.get("/api/auth/me")
    owner_user_id = UUID(me.json()["id"])
    definition, version = await _create_definition_and_version(
        admin_client,
        seed,
        suffix="workflow",
    )

    claimed_publisher = await admin_client.post(
        f"/api/v1/task-agent/definitions/{definition['id']}/versions",
        json={
            "specification": {"model": "caller-claimed"},
            "published_by_principal_id": str(seed.other_principal_id),
        },
    )
    assert claimed_publisher.status_code == 422

    agent_session, run, question = await _create_waiting_question(
        admin_client,
        seed,
        version["id"],
        suffix="workflow-run",
    )
    claimed_requester = await admin_client.post(
        "/api/v1/task-agent/sessions",
        json={
            "workspace_id": str(seed.workspace_id),
            "agent_version_id": version["id"],
            "requester_principal_id": str(seed.other_principal_id),
        },
    )
    assert claimed_requester.status_code == 422

    old_answer = await admin_client.post(
        f"/api/v1/task-agent/questions/{question['id']}/answer",
        json={
            "principal_id": str(seed.allowed_principal_id),
            "answer_payload": {"answer": "yes"},
        },
    )
    invalid_override = await admin_client.post(
        f"/api/v1/task-agent/questions/{question['id']}/override-answer",
        json={"answer_payload": {"answer": "yes"}, "reason": "   "},
    )
    assert old_answer.status_code == 404
    assert invalid_override.status_code == 422

    override_reason = "Owner confirmed the requested operation"
    override = await admin_client.post(
        f"/api/v1/task-agent/questions/{question['id']}/override-answer",
        json={"answer_payload": {"answer": "yes"}, "reason": override_reason},
    )
    assert override.status_code == 200
    assert override.json()["run"]["status"] == "QUEUED"

    invalid_transition = await admin_client.post(
        f"/api/v1/task-agent/runs/{run['id']}/transition",
        json={"status": "WAITING_USER"},
    )
    assert invalid_transition.status_code == 409
    running_again = await admin_client.post(
        f"/api/v1/task-agent/runs/{run['id']}/transition",
        json={"status": "RUNNING"},
    )
    assert running_again.status_code == 200
    second_question = await admin_client.post(
        f"/api/v1/task-agent/runs/{run['id']}/questions",
        json={
            "allowed_principal_id": str(seed.allowed_principal_id),
            "prompt": "Second question",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
    )
    assert second_question.status_code == 201

    state = await admin_client.get(
        f"/api/v1/task-agent/sessions/{agent_session['id']}/state",
        params={"history_limit": 1},
    )
    assert state.status_code == 200
    state_payload = state.json()
    assert len(state_payload["inbox"]) == 1
    assert len(state_payload["events"]) == 1
    assert len(state_payload["questions"]) == 1
    assert state_payload["inbox_has_more"] is True
    assert state_payload["events_has_more"] is True
    assert state_payload["questions_has_more"] is True
    assert state_payload["questions"][0]["id"] == second_question.json()["id"]
    assert (
        await admin_client.get(
            f"/api/v1/task-agent/sessions/{agent_session['id']}/state",
            params={"history_limit": 0},
        )
    ).status_code == 422

    async with app.state.database.session_factory() as database:
        admin_principals = list(
            await database.scalars(
                select(Principal).where(
                    Principal.workspace_id == seed.workspace_id,
                    Principal.principal_type == PrincipalType.ADMIN_USER,
                    Principal.external_id == str(owner_user_id),
                )
            )
        )
        stored_session = await database.get(AgentSession, UUID(str(agent_session["id"])))
        stored_question = await database.get(PendingQuestion, UUID(str(question["id"])))
        answer_inbox = await database.scalar(
            select(AgentSessionInbox).where(
                AgentSessionInbox.question_id == UUID(str(question["id"]))
            )
        )
        answer_event = await database.scalar(
            select(AgentEvent).where(
                AgentEvent.question_id == UUID(str(question["id"])),
                AgentEvent.event_type == AgentEventType.QUESTION_ANSWERED,
            )
        )
        audits = list(
            await database.scalars(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == seed.workspace_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )

    assert len(admin_principals) == 1
    admin_principal = admin_principals[0]
    assert stored_session is not None
    assert stored_session.requester_principal_id == admin_principal.id
    assert version["published_by_principal_id"] == str(admin_principal.id)
    assert stored_question is not None
    assert stored_question.allowed_principal_id == seed.allowed_principal_id
    assert stored_question.answered_by_principal_id == admin_principal.id
    assert answer_inbox is not None
    assert answer_inbox.actor_principal_id == admin_principal.id
    assert answer_event is not None
    assert answer_event.payload["answer_mode"] == "ADMIN_OVERRIDE"
    assert answer_event.payload["actor_principal_id"] == str(admin_principal.id)
    assert answer_event.payload["reason"] == override_reason
    assert {event.actor_id for event in audits} == {str(owner_user_id)}
    assert any(
        event.action == "task_agent.run.transition" and event.result == "FAILURE"
        for event in audits
    )
    assert any(
        event.action == "task_agent.question.override_answer" and event.result == "SUCCESS"
        for event in audits
    )


async def test_context_and_lists_resolve_the_only_workspace_without_connection_permission(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="reader")
    await _create_definition_and_version(admin_client, seed, suffix="reader")
    username, _ = await _create_operator(
        admin_client,
        suffix="reader",
        permission_codes=[AGENT_READ_PERMISSION],
    )
    await _login_operator(admin_client, username)

    context = await admin_client.get("/api/v1/task-agent/context")
    definitions = await admin_client.get("/api/v1/task-agent/definitions")
    sessions = await admin_client.get("/api/v1/task-agent/sessions")
    wrong_workspace = await admin_client.get(
        "/api/v1/task-agent/definitions",
        params={"workspace_id": str(uuid4())},
    )
    write = await admin_client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(seed.workspace_id),
            "definition_key": "reader-denied",
            "name": "Reader denied",
        },
    )
    assert context.status_code == 200
    assert context.json() == {
        "workspace_id": str(seed.workspace_id),
        "workspace_name": "Agent API reader",
    }
    assert definitions.status_code == 200
    assert definitions.json()["total"] == 1
    assert sessions.status_code == 200
    assert wrong_workspace.status_code == 404
    assert write.status_code == 403


async def test_override_requires_its_dedicated_permission(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="override-permission")
    _, version = await _create_definition_and_version(
        admin_client,
        seed,
        suffix="override-permission",
    )
    _, _, question = await _create_waiting_question(
        admin_client,
        seed,
        version["id"],
        suffix="override-permission-run",
    )
    runner_username, _ = await _create_operator(
        admin_client,
        suffix="runner-only",
        permission_codes=[AGENT_RUN_PERMISSION],
    )
    override_username, override_user_id = await _create_operator(
        admin_client,
        suffix="override-only",
        permission_codes=[AGENT_QUESTION_OVERRIDE_PERMISSION],
    )

    await _login_operator(admin_client, runner_username)
    denied = await admin_client.post(
        f"/api/v1/task-agent/questions/{question['id']}/override-answer",
        json={"answer_payload": {"answer": "runner"}, "reason": "Must be denied"},
    )
    assert denied.status_code == 403

    await _login_operator(admin_client, override_username)
    answered = await admin_client.post(
        f"/api/v1/task-agent/questions/{question['id']}/override-answer",
        json={"answer_payload": {"answer": "override"}, "reason": "Approved by operator"},
    )
    assert answered.status_code == 200
    answered_principal_id = UUID(answered.json()["question"]["answered_by_principal_id"])
    async with app.state.database.session_factory() as database:
        principal = await database.get(Principal, answered_principal_id)
    assert principal is not None
    assert principal.external_id == str(override_user_id)
    assert principal.id != seed.allowed_principal_id


async def test_write_and_override_permissions_are_independent(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="writer")
    username, _ = await _create_operator(
        admin_client,
        suffix="writer",
        permission_codes=[AGENT_WRITE_PERMISSION],
    )
    await _login_operator(admin_client, username)
    create = await admin_client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(seed.workspace_id),
            "definition_key": "writer-created",
            "name": "Writer created",
        },
    )
    listing = await admin_client.get("/api/v1/task-agent/definitions")
    session_create = await admin_client.post(
        "/api/v1/task-agent/sessions",
        json={"workspace_id": str(seed.workspace_id), "agent_version_id": str(uuid4())},
    )
    assert create.status_code == 201
    assert listing.status_code == 403
    assert session_create.status_code == 403


async def test_workspace_mismatch_and_domain_failures_are_audited(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="audit-failure")
    mismatch = await admin_client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(uuid4()),
            "definition_key": "wrong-workspace",
            "name": "Wrong workspace",
        },
    )
    definition, _ = await _create_definition_and_version(
        admin_client,
        seed,
        suffix="audit-failure",
    )
    duplicate = await admin_client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(seed.workspace_id),
            "definition_key": definition["definition_key"],
            "name": "Duplicate",
        },
    )
    assert mismatch.status_code == 404
    assert duplicate.status_code == 409

    async with app.state.database.session_factory() as database:
        failures = list(
            await database.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == seed.workspace_id,
                    AuditEvent.action == "task_agent.definition.create",
                    AuditEvent.result == "FAILURE",
                )
            )
        )
    assert len(failures) == 2
    assert {event.detail["error_type"] for event in failures} == {
        "TaskAgentNotFoundError",
        "TaskAgentConflictError",
    }


async def test_private_reasoning_is_rejected_and_recursively_redacted(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="redaction")
    definition_response = await admin_client.post(
        "/api/v1/task-agent/definitions",
        json={
            "workspace_id": str(seed.workspace_id),
            "definition_key": "redaction",
            "name": "Redaction",
        },
    )
    definition_id = UUID(definition_response.json()["id"])
    rejected = await admin_client.post(
        f"/api/v1/task-agent/definitions/{definition_id}/versions",
        json={
            "specification": {
                "nested": [{"internalReasoning": "private"}],
                "result": "public",
            }
        },
    )
    assert rejected.status_code == 422

    specification = {
        "result": "visible",
        "nested": [
            {"analysis": "private analysis", "safe": "one"},
            {"reasoningDetails": "private details", "safe": "two"},
            {"internal-reasoning": "private internal", "safe": "three"},
            {"thinking": "private thought", "reasoning_content": "private content"},
        ],
        "chainOfThought": "private chain",
    }
    canonical = json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    async with app.state.database.session_factory() as database, database.begin():
        version = AgentVersion(
            definition_id=definition_id,
            version_number=1,
            specification=specification,
            specification_sha256=hashlib.sha256(canonical).hexdigest(),
            published_by_principal_id=None,
            published_at=datetime.now(UTC),
        )
        database.add(version)
        await database.flush()
        version_id = version.id

    response = await admin_client.get(f"/api/v1/task-agent/versions/{version_id}")
    assert response.status_code == 200
    assert response.json()["specification"] == {
        "result": "visible",
        "nested": [
            {"safe": "one"},
            {"safe": "two"},
            {"safe": "three"},
            {},
        ],
    }
    for private_value in (
        "private analysis",
        "private details",
        "private internal",
        "private thought",
        "private content",
        "private chain",
    ):
        assert private_value not in response.text


async def test_task_agent_writes_require_csrf(
    app: FastAPI,
    admin_client: AsyncClient,
) -> None:
    seed = await _seed_workspace(app, suffix="csrf")
    csrf_token = admin_client.headers.pop(CSRF_HEADER_NAME)
    try:
        response = await admin_client.post(
            "/api/v1/task-agent/definitions",
            json={
                "workspace_id": str(seed.workspace_id),
                "definition_key": "csrf-denied",
                "name": "CSRF denied",
            },
        )
    finally:
        admin_client.headers[CSRF_HEADER_NAME] = csrf_token
    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed"


async def test_permission_catalog_contains_task_agent_capabilities(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/api/v1/admin/permissions")
    codes = {item["code"] for item in response.json()["items"]}
    assert {
        AGENT_READ_PERMISSION,
        AGENT_WRITE_PERMISSION,
        AGENT_RUN_PERMISSION,
        AGENT_QUESTION_OVERRIDE_PERMISSION,
    } <= codes
