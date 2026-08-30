from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from wechat_bot.auth.constants import (
    AGENT_QUESTION_OVERRIDE_PERMISSION,
    AGENT_READ_PERMISSION,
    AGENT_RUN_PERMISSION,
    AGENT_WRITE_PERMISSION,
)
from wechat_bot.auth.dependencies import (
    CurrentPrincipalDependency,
    DatabaseSessionDependency,
    require_management_request,
    require_permission,
)
from wechat_bot.auth.service import AuthPrincipal
from wechat_bot.db.models import AuditEvent, Workspace
from wechat_bot.db.policy_models import Principal
from wechat_bot.task_agent.schemas import (
    AgentContextView,
    AgentDefinitionCreate,
    AgentDefinitionList,
    AgentDefinitionView,
    AgentRunCreate,
    AgentRunList,
    AgentRunTransition,
    AgentRunView,
    AgentSessionCreate,
    AgentSessionCreateRequest,
    AgentSessionList,
    AgentSessionStateView,
    AgentSessionView,
    AgentVersionList,
    AgentVersionPublish,
    AgentVersionPublishRequest,
    AgentVersionView,
    PendingQuestionCreate,
    PendingQuestionOverrideAnswer,
    PendingQuestionView,
    QuestionAnswerResult,
)
from wechat_bot.task_agent.service import (
    PersistedJsonValidationError,
    QuestionAnswerForbiddenError,
    TaskAgentConflictError,
    TaskAgentError,
    TaskAgentNotFoundError,
    TaskAgentService,
)

router = APIRouter(
    prefix="/api/v1/task-agent",
    tags=["Task Agent"],
    dependencies=[Depends(require_management_request)],
)
service = TaskAgentService()


@router.get(
    "/context",
    response_model=AgentContextView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_task_agent_context(database: DatabaseSessionDependency) -> AgentContextView:
    workspace = await _resolve_workspace(database)
    return AgentContextView(workspace_id=workspace.id, workspace_name=workspace.name)


@router.get(
    "/definitions",
    response_model=AgentDefinitionList,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def list_agent_definitions(
    database: DatabaseSessionDependency,
    workspace_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentDefinitionList:
    workspace = await _resolve_workspace(database, requested_id=workspace_id)
    items, total = await service.list_definitions(
        database,
        workspace_id=workspace.id,
        limit=limit,
        offset=offset,
    )
    return AgentDefinitionList(
        items=[AgentDefinitionView.model_validate(item) for item in items],
        total=total,
    )


@router.post(
    "/definitions",
    response_model=AgentDefinitionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(AGENT_WRITE_PERMISSION))],
)
async def create_agent_definition(
    payload: AgentDefinitionCreate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AgentDefinitionView:
    workspace = await _resolve_workspace(database)
    try:
        _require_requested_workspace(workspace, payload.workspace_id)
        definition = await service.create_definition(database, payload)
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.definition.create",
            object_type="agent_definition",
            object_id=payload.definition_key,
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.definition.create",
        object_type="agent_definition",
        object_id=str(definition.id),
        result="SUCCESS",
    )
    await database.commit()
    return AgentDefinitionView.model_validate(definition)


@router.get(
    "/definitions/{definition_id}",
    response_model=AgentDefinitionView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_agent_definition(
    definition_id: UUID,
    database: DatabaseSessionDependency,
) -> AgentDefinitionView:
    workspace = await _resolve_workspace(database)
    try:
        definition = await service.get_definition(
            database,
            definition_id,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentDefinitionView.model_validate(definition)


@router.get(
    "/definitions/{definition_id}/versions",
    response_model=AgentVersionList,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def list_agent_versions(
    definition_id: UUID,
    database: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentVersionList:
    workspace = await _resolve_workspace(database)
    try:
        items, total = await service.list_versions(
            database,
            definition_id,
            workspace_id=workspace.id,
            limit=limit,
            offset=offset,
        )
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentVersionList(
        items=[AgentVersionView.model_validate(item) for item in items],
        total=total,
    )


@router.post(
    "/definitions/{definition_id}/versions",
    response_model=AgentVersionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(AGENT_WRITE_PERMISSION))],
)
async def publish_agent_version(
    definition_id: UUID,
    payload: AgentVersionPublishRequest,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AgentVersionView:
    workspace = await _resolve_workspace(database)
    try:
        principal = await _admin_principal(database, workspace.id, actor)
        version = await service.publish_version(
            database,
            definition_id,
            AgentVersionPublish(
                specification=payload.specification,
                published_by_principal_id=principal.id,
            ),
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.version.publish",
            object_type="agent_definition",
            object_id=str(definition_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.version.publish",
        object_type="agent_version",
        object_id=str(version.id),
        result="SUCCESS",
    )
    await database.commit()
    return AgentVersionView.model_validate(version)


@router.get(
    "/versions/{version_id}",
    response_model=AgentVersionView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_agent_version(
    version_id: UUID,
    database: DatabaseSessionDependency,
) -> AgentVersionView:
    workspace = await _resolve_workspace(database)
    try:
        version = await service.get_version(database, version_id, workspace_id=workspace.id)
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentVersionView.model_validate(version)


@router.get(
    "/sessions",
    response_model=AgentSessionList,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def list_agent_sessions(
    database: DatabaseSessionDependency,
    workspace_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentSessionList:
    workspace = await _resolve_workspace(database, requested_id=workspace_id)
    items, total = await service.list_sessions(
        database,
        workspace_id=workspace.id,
        limit=limit,
        offset=offset,
    )
    return AgentSessionList(
        items=[AgentSessionView.model_validate(item) for item in items],
        total=total,
    )


@router.post(
    "/sessions",
    response_model=AgentSessionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(AGENT_RUN_PERMISSION))],
)
async def create_agent_session(
    payload: AgentSessionCreateRequest,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AgentSessionView:
    workspace = await _resolve_workspace(database)
    try:
        _require_requested_workspace(workspace, payload.workspace_id)
        principal = await _admin_principal(database, workspace.id, actor)
        agent_session = await service.create_session(
            database,
            AgentSessionCreate(
                workspace_id=workspace.id,
                agent_version_id=payload.agent_version_id,
                requester_principal_id=principal.id,
                task_scope=payload.task_scope,
            ),
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.session.create",
            object_type="agent_version",
            object_id=str(payload.agent_version_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.session.create",
        object_type="agent_session",
        object_id=str(agent_session.id),
        result="SUCCESS",
    )
    await database.commit()
    return AgentSessionView.model_validate(agent_session)


@router.get(
    "/sessions/{session_id}",
    response_model=AgentSessionView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_agent_session(
    session_id: UUID,
    database: DatabaseSessionDependency,
) -> AgentSessionView:
    workspace = await _resolve_workspace(database)
    try:
        agent_session = await service.get_session(
            database,
            session_id,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentSessionView.model_validate(agent_session)


@router.get(
    "/sessions/{session_id}/state",
    response_model=AgentSessionStateView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_agent_session_state(
    session_id: UUID,
    database: DatabaseSessionDependency,
    history_limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AgentSessionStateView:
    workspace = await _resolve_workspace(database)
    try:
        return await service.get_session_state(
            database,
            session_id,
            workspace_id=workspace.id,
            history_limit=history_limit,
        )
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc


@router.get(
    "/sessions/{session_id}/runs",
    response_model=AgentRunList,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def list_agent_runs(
    session_id: UUID,
    database: DatabaseSessionDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AgentRunList:
    workspace = await _resolve_workspace(database)
    try:
        items, total = await service.list_runs(
            database,
            session_id,
            workspace_id=workspace.id,
            limit=limit,
            offset=offset,
        )
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentRunList(
        items=[AgentRunView.model_validate(item) for item in items],
        total=total,
    )


@router.post(
    "/sessions/{session_id}/runs",
    response_model=AgentRunView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(AGENT_RUN_PERMISSION))],
)
async def create_agent_run(
    session_id: UUID,
    payload: AgentRunCreate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AgentRunView:
    workspace = await _resolve_workspace(database)
    try:
        run = await service.create_run(
            database,
            session_id,
            payload,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.run.create",
            object_type="agent_session",
            object_id=str(session_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.run.create",
        object_type="agent_run",
        object_id=str(run.id),
        result="SUCCESS",
    )
    await database.commit()
    return AgentRunView.model_validate(run)


@router.get(
    "/runs/{run_id}",
    response_model=AgentRunView,
    dependencies=[Depends(require_permission(AGENT_READ_PERMISSION))],
)
async def get_agent_run(
    run_id: UUID,
    database: DatabaseSessionDependency,
) -> AgentRunView:
    workspace = await _resolve_workspace(database)
    try:
        run = await service.get_run(database, run_id, workspace_id=workspace.id)
    except TaskAgentError as exc:
        raise _task_agent_http_error(exc) from exc
    return AgentRunView.model_validate(run)


@router.post(
    "/runs/{run_id}/transition",
    response_model=AgentRunView,
    dependencies=[Depends(require_permission(AGENT_RUN_PERMISSION))],
)
async def transition_agent_run(
    run_id: UUID,
    payload: AgentRunTransition,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> AgentRunView:
    workspace = await _resolve_workspace(database)
    try:
        run = await service.transition_run(
            database,
            run_id,
            payload,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.run.transition",
            object_type="agent_run",
            object_id=str(run_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.run.transition",
        object_type="agent_run",
        object_id=str(run.id),
        result="SUCCESS",
        detail={"status": run.status.value},
    )
    await database.commit()
    return AgentRunView.model_validate(run)


@router.post(
    "/runs/{run_id}/questions",
    response_model=PendingQuestionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(AGENT_RUN_PERMISSION))],
)
async def create_pending_question(
    run_id: UUID,
    payload: PendingQuestionCreate,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> PendingQuestionView:
    workspace = await _resolve_workspace(database)
    try:
        question = await service.ask_question(
            database,
            run_id,
            payload,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.question.create",
            object_type="agent_run",
            object_id=str(run_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.question.create",
        object_type="agent_pending_question",
        object_id=str(question.id),
        result="SUCCESS",
    )
    await database.commit()
    return PendingQuestionView.model_validate(question)


@router.post(
    "/questions/{question_id}/override-answer",
    response_model=QuestionAnswerResult,
    dependencies=[Depends(require_permission(AGENT_QUESTION_OVERRIDE_PERMISSION))],
)
async def override_pending_question(
    question_id: UUID,
    payload: PendingQuestionOverrideAnswer,
    database: DatabaseSessionDependency,
    actor: CurrentPrincipalDependency,
) -> QuestionAnswerResult:
    workspace = await _resolve_workspace(database)
    try:
        principal = await _admin_principal(database, workspace.id, actor)
        result = await service.override_answer_question(
            database,
            question_id,
            payload,
            actor_principal_id=principal.id,
            workspace_id=workspace.id,
        )
    except TaskAgentError as exc:
        await _audit_failure(
            database,
            workspace_id=workspace.id,
            actor=actor,
            action="task_agent.question.override_answer",
            object_type="agent_pending_question",
            object_id=str(question_id),
            exc=exc,
        )
        raise _task_agent_http_error(exc) from exc
    _record_audit(
        database,
        workspace_id=workspace.id,
        actor=actor,
        action="task_agent.question.override_answer",
        object_type="agent_pending_question",
        object_id=str(question_id),
        result="SUCCESS",
        detail={"reason": payload.reason},
    )
    await database.commit()
    return result


async def _admin_principal(
    database: DatabaseSessionDependency,
    workspace_id: UUID,
    actor: AuthPrincipal,
) -> Principal:
    return await service.resolve_admin_principal(
        database,
        workspace_id=workspace_id,
        admin_user_id=actor.user_id,
        display_name=actor.display_name or actor.username,
    )


async def _resolve_workspace(
    database: DatabaseSessionDependency,
    *,
    requested_id: UUID | None = None,
) -> Workspace:
    workspaces = list(await database.scalars(select(Workspace).order_by(Workspace.id).limit(2)))
    if not workspaces:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )
    if len(workspaces) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this release supports exactly one workspace",
        )
    workspace = workspaces[0]
    if requested_id is not None and requested_id != workspace.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        )
    return workspace


def _require_requested_workspace(workspace: Workspace, requested_id: UUID) -> None:
    if workspace.id != requested_id:
        raise TaskAgentNotFoundError("workspace not found")


def _record_audit(
    database: DatabaseSessionDependency,
    *,
    workspace_id: UUID,
    actor: AuthPrincipal,
    action: str,
    object_type: str,
    object_id: str,
    result: str,
    detail: dict[str, Any] | None = None,
) -> None:
    database.add(
        AuditEvent(
            workspace_id=workspace_id,
            trace_id=None,
            actor_type="ADMIN_USER",
            actor_id=str(actor.user_id),
            action=action,
            object_type=object_type,
            object_id=object_id,
            result=result,
            detail=detail or {},
        )
    )


async def _audit_failure(
    database: DatabaseSessionDependency,
    *,
    workspace_id: UUID,
    actor: AuthPrincipal,
    action: str,
    object_type: str,
    object_id: str,
    exc: TaskAgentError,
) -> None:
    await database.rollback()
    _record_audit(
        database,
        workspace_id=workspace_id,
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result="FAILURE",
        detail={"error_type": type(exc).__name__, "error": str(exc)},
    )
    await database.commit()


def _task_agent_http_error(exc: TaskAgentError) -> HTTPException:
    if isinstance(exc, QuestionAnswerForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, TaskAgentNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, PersistedJsonValidationError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )
    if isinstance(exc, TaskAgentConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="task-agent operation failed",
    )
