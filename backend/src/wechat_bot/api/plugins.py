from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.dependencies import require_management_request, require_permission
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.plugins.catalog import (
    PLUGIN_REDACTION_MARKER,
    PluginActivationError,
    PluginCatalogError,
    PluginCatalogService,
    PluginObjectNotFoundError,
)
from wechat_bot.plugins.schemas import (
    BuiltinPluginInstall,
    PluginActivationResult,
    PluginActivationView,
    PluginCatalogView,
    PluginContextView,
    PluginDeploymentCreate,
    PluginDeploymentResult,
    PluginDeploymentView,
    PluginInstallResult,
    PluginInvocation,
    PluginInvocationResult,
    PluginPackageView,
    PluginRevisionCreate,
    PluginRevisionDraft,
    PluginRevisionView,
    PluginView,
)
from wechat_bot.plugins.supervisor import PluginNotActiveError, PluginSupervisor

router = APIRouter(
    prefix="/api/v1/plugins",
    tags=["Plugins"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission("plugin.read")),
    ],
)


@router.get("/context", response_model=PluginContextView)
async def get_plugin_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginContextView:
    try:
        workspace = await _service(request).workspace_context(session)
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginCatalogError as exc:
        raise _conflict(exc) from exc
    return PluginContextView(workspace_id=workspace.id, name=workspace.name)


@router.post(
    "/builtins/{plugin_id}/install",
    response_model=PluginInstallResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("plugin.deploy"))],
)
async def install_builtin_plugin(
    plugin_id: str,
    payload: BuiltinPluginInstall,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginInstallResult:
    service = _service(request)
    try:
        plugin, package = await service.install_builtin(
            session,
            workspace_id=payload.workspace_id,
            plugin_id=plugin_id,
        )
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginCatalogError as exc:
        raise _conflict(exc) from exc
    await session.commit()
    return PluginInstallResult(
        plugin=PluginView.model_validate(plugin),
        package=PluginPackageView.model_validate(package),
    )


@router.get("", response_model=PluginCatalogView)
async def get_plugin_catalog(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginCatalogView:
    plugins, packages, deployments, revisions = await _service(request).catalog(session)
    return PluginCatalogView(
        plugins=[PluginView.model_validate(item) for item in plugins],
        packages=[PluginPackageView.model_validate(item) for item in packages],
        deployments=[PluginDeploymentView.model_validate(item) for item in deployments],
        revisions=[PluginRevisionView.model_validate(item) for item in revisions],
    )


@router.post(
    "/deployments",
    response_model=PluginDeploymentResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("plugin.deploy"))],
)
async def create_plugin_deployment(
    payload: PluginDeploymentCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginDeploymentResult:
    try:
        deployment, revision = await _service(request).create_deployment(session, payload)
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginCatalogError as exc:
        raise _conflict(exc) from exc
    await session.commit()
    return PluginDeploymentResult(
        deployment=PluginDeploymentView.model_validate(deployment),
        revision=PluginRevisionView.model_validate(revision),
    )


@router.post(
    "/deployments/{deployment_id}/revisions",
    response_model=PluginRevisionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("plugin.deploy"))],
)
async def create_plugin_revision(
    deployment_id: UUID,
    payload: PluginRevisionCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginRevisionView:
    try:
        revision = await _service(request).create_revision(session, deployment_id, payload)
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginCatalogError as exc:
        raise _conflict(exc) from exc
    await session.commit()
    return PluginRevisionView.model_validate(revision)


@router.get(
    "/deployments/{deployment_id}/revisions/{revision_id}/draft",
    response_model=PluginRevisionDraft,
)
async def get_plugin_revision_draft(
    deployment_id: UUID,
    revision_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginRevisionDraft:
    service = _service(request)
    try:
        revision, config = await service.revision_draft(
            session,
            deployment_id=deployment_id,
            revision_id=revision_id,
        )
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginCatalogError as exc:
        raise _conflict(exc) from exc
    return PluginRevisionDraft(
        source_revision_id=revision.id,
        package_version_id=revision.package_version_id,
        config=config,
        scope=revision.scope,
        grants=revision.grants,
        secret_placeholder=PLUGIN_REDACTION_MARKER,
    )


@router.post(
    "/deployments/{deployment_id}/revisions/{revision_id}/activate",
    response_model=PluginActivationResult,
    dependencies=[Depends(require_permission("plugin.deploy"))],
)
async def activate_plugin_revision(
    deployment_id: UUID,
    revision_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginActivationResult:
    supervisor: PluginSupervisor = request.app.state.plugin_supervisor
    try:
        transition = await _service(request).activate_revision(
            session,
            supervisor=supervisor,
            deployment_id=deployment_id,
            revision_id=revision_id,
        )
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginActivationError as exc:
        await session.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    try:
        await session.commit()
    except BaseException:
        await supervisor.abort_activation(transition.runtime)
        raise
    await supervisor.commit_activation(transition.runtime)
    return PluginActivationResult(
        deployment=PluginDeploymentView.model_validate(transition.deployment),
        activation=PluginActivationView.model_validate(transition.activation),
    )


@router.post(
    "/deployments/{deployment_id}/deactivate",
    response_model=PluginDeploymentView,
    dependencies=[Depends(require_permission("plugin.deploy"))],
)
async def deactivate_plugin(
    deployment_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PluginDeploymentView:
    supervisor: PluginSupervisor = request.app.state.plugin_supervisor
    try:
        transition = await _service(request).deactivate(
            session,
            supervisor=supervisor,
            deployment_id=deployment_id,
        )
    except PluginObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except PluginActivationError as exc:
        raise _conflict(exc) from exc
    try:
        await session.commit()
    except BaseException:
        await supervisor.abort_deactivation(transition.runtime)
        raise
    await supervisor.commit_deactivation(transition.runtime)
    return PluginDeploymentView.model_validate(transition.deployment)


@router.post(
    "/deployments/{deployment_id}/invoke",
    response_model=PluginInvocationResult,
    dependencies=[Depends(require_permission("plugin.invoke"))],
)
async def invoke_plugin(
    deployment_id: UUID,
    payload: PluginInvocation,
    request: Request,
) -> PluginInvocationResult:
    supervisor: PluginSupervisor = request.app.state.plugin_supervisor
    try:
        epoch, result = await supervisor.call(
            str(deployment_id),
            payload.method,
            payload.params,
        )
    except PluginNotActiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PluginInvocationResult(activation_epoch=epoch, result=result)


def _service(request: Request) -> PluginCatalogService:
    settings: Settings = request.app.state.settings
    return PluginCatalogService(CredentialCipher.from_settings(settings))


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
