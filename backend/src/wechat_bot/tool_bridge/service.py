from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError
from wechat_bot.db.base import utc_now
from wechat_bot.db.maibot_models import (
    MaiBotBridgeDirection,
    MaiBotBridgeEnvelope,
    MaiBotBridgeKind,
    MaiBotBridgeStatus,
)
from wechat_bot.db.models import (
    AuditEvent,
    BotAccount,
    Chatroom,
    ChatroomMembership,
    Contact,
    GeweConnection,
    NormalizedEvent,
    WebhookInbox,
)
from wechat_bot.db.plugin_models import (
    Plugin,
    PluginActivationStatus,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginPackageStatus,
    PluginPackageVersion,
    PluginRevisionActivation,
)
from wechat_bot.db.policy_models import AclResourceType, Principal, PrincipalType
from wechat_bot.db.tool_models import ToolCall, ToolCallStatus, ToolInvocationMode
from wechat_bot.maibot.constants import MAIBOT_CONNECTOR_PLUGIN_ID, MAIBOT_FORWARD_CAPABILITY
from wechat_bot.maibot.schemas import MaiBotConversationContextClaims
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.plugins.manifest import PluginManifest, PluginTool
from wechat_bot.plugins.supervisor import PluginRuntimeError
from wechat_bot.policy.schemas import AclEvaluationRequest
from wechat_bot.policy.service import (
    InvalidPolicyRuleError,
    PolicyObjectNotFoundError,
    PolicyService,
)

MAX_ARGUMENT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 128 * 1024


class ToolInvoker(Protocol):
    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]: ...


class ToolBrokerError(RuntimeError):
    """Base error for a broker operation that can be shown without internals."""


class ToolCallNotFoundError(ToolBrokerError, LookupError):
    pass


class ToolCallIdempotencyConflictError(ToolBrokerError):
    pass


class ToolExecutionDeniedError(ToolBrokerError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ToolStaleActivationError(ToolBrokerError):
    pass


class ToolInputValidationError(ToolBrokerError):
    pass


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    call: ToolCall
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _ConnectorContext:
    deployment: PluginDeployment
    revision: PluginDeploymentRevision
    activation: PluginRevisionActivation
    source: MaiBotBridgeEnvelope
    account: BotAccount
    chatroom: Chatroom | None
    contact: Contact | None
    principal: Principal
    authorization: OutboxAuthorizationContext
    context_digest: str


@dataclass(frozen=True, slots=True)
class _ToolRoute:
    deployment: PluginDeployment
    plugin: Plugin
    revision: PluginDeploymentRevision
    package: PluginPackageVersion
    activation: PluginRevisionActivation
    manifest: PluginManifest
    tool: PluginTool


class ToolBrokerService:
    """Single execution boundary shared by MaiBot and future Task Agent workers.

    The caller may be an untrusted model runtime.  Every request is bound to a
    platform-issued connector context and is authorized again immediately before
    the plugin runner is called.  No GeWe credential is ever passed to a plugin.
    """

    def __init__(
        self,
        cipher: CredentialCipher,
        invoker: ToolInvoker,
        *,
        policy_service: PolicyService | None = None,
        clock: Callable[[], datetime] = utc_now,
        max_argument_bytes: int = MAX_ARGUMENT_BYTES,
        max_result_bytes: int = MAX_RESULT_BYTES,
    ) -> None:
        if max_argument_bytes < 1 or max_result_bytes < 1:
            raise ValueError("Tool payload limits must be positive")
        self._cipher = cipher
        self._invoker = invoker
        self._policy = policy_service or PolicyService()
        self._clock = clock
        self._max_argument_bytes = max_argument_bytes
        self._max_result_bytes = max_result_bytes

    async def list_visible_tools(
        self,
        session: AsyncSession,
        *,
        deployment_revision_id: UUID,
        activation_epoch: int,
        connector_context_id: str,
    ) -> list[dict[str, Any]]:
        """Return a filtered catalog; visibility is never treated as authorization."""

        context = await self._resolve_connector_context(
            session,
            deployment_revision_id=deployment_revision_id,
            activation_epoch=activation_epoch,
            connector_context_id=connector_context_id,
        )
        await self._authorize_connector(session, context)
        allowlist = await self._connector_tool_allowlist(context.revision)
        if not allowlist:
            return []

        routes = await self._tool_routes(session, workspace_id=context.deployment.workspace_id)
        visible: list[dict[str, Any]] = []
        for route in routes:
            tool = route.tool
            if tool.name not in allowlist:
                continue
            if not self._route_is_usable(route, context):
                continue
            if not await self._target_allowed(session, context, route):
                continue
            visible.append(
                {
                    "tool_name": tool.name,
                    "tool_schema_version": tool.schema_version,
                    "plugin_id": route.manifest.plugin_id,
                    "plugin_name": route.plugin.name,
                    "deployment_id": route.deployment.id,
                    "revision_id": route.revision.id,
                    "description": tool.description,
                    "effect_class": tool.effect_class,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                    "required_capabilities": tool.required_capabilities,
                }
            )
        return visible

    async def list_catalog(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
    ) -> list[dict[str, Any]]:
        """List active Tool declarations for management inspection.

        This is deliberately not an authorization decision. Runtime callers must
        use :meth:`list_visible_tools` or :meth:`invoke`, which bind a caller
        context and evaluate ACLs again.
        """
        return [
            {
                "tool_name": route.tool.name,
                "tool_schema_version": route.tool.schema_version,
                "plugin_id": route.manifest.plugin_id,
                "plugin_name": route.plugin.name,
                "deployment_id": route.deployment.id,
                "revision_id": route.revision.id,
                "description": route.tool.description,
                "effect_class": route.tool.effect_class,
                "input_schema": route.tool.input_schema,
                "output_schema": route.tool.output_schema,
                "required_capabilities": route.tool.required_capabilities,
            }
            for route in await self._tool_routes(session, workspace_id=workspace_id)
        ]

    async def invoke(
        self,
        session: AsyncSession,
        *,
        request: Any,
    ) -> ToolCallResult:
        """Execute one Tool request and persist its closed outcome.

        ``request`` is intentionally duck-typed so the broker can be reused by
        the MaiBot WebSocket mapper and a Task Agent worker without importing
        either transport layer.  It must expose the fields of ToolCallRequest.
        """

        arguments, arguments_sha256 = self._canonical_arguments(request.arguments)
        context = await self._resolve_connector_context(
            session,
            deployment_revision_id=request.deployment_revision_id,
            activation_epoch=request.activation_epoch,
            connector_context_id=request.connector_context_id,
        )
        existing = await session.scalar(
            select(ToolCall).where(
                ToolCall.connector_revision_id == context.revision.id,
                ToolCall.external_tool_call_id == request.external_tool_call_id,
            )
        )
        if existing is not None:
            if (
                existing.arguments_sha256 != arguments_sha256
                or existing.tool_name != request.tool_name
            ):
                raise ToolCallIdempotencyConflictError(
                    "external Tool call id is already bound to different arguments"
                )
            return ToolCallResult(existing, replayed=True)

        if request.invocation_mode is not ToolInvocationMode.USER_REQUESTED:
            return await self._record_rejected(
                session,
                request=request,
                context=context,
                arguments=arguments,
                arguments_sha256=arguments_sha256,
                code="TOOL_AUTONOMOUS_DISABLED",
                detail="autonomous MaiBot Tool calls are disabled in MVP",
            )

        call = ToolCall(
            workspace_id=context.deployment.workspace_id,
            connector_deployment_id=context.deployment.id,
            connector_revision_id=context.revision.id,
            connector_activation_id=context.activation.id,
            external_tool_call_id=request.external_tool_call_id,
            connector_context_digest=context.context_digest,
            tool_name=request.tool_name,
            tool_schema_version=request.tool_schema_version,
            invocation_mode=request.invocation_mode,
            arguments=arguments,
            arguments_sha256=arguments_sha256,
            trace_id=context.source.trace_id,
            actor_principal_id=context.principal.id,
            bot_account_id=context.account.id,
            chatroom_id=context.chatroom.id if context.chatroom is not None else None,
            contact_id=context.contact.id if context.contact is not None else None,
            status=ToolCallStatus.RECEIVED,
            deadline_at=request.deadline_at,
            available_at=self._now(),
        )
        try:
            async with session.begin_nested():
                session.add(call)
                await session.flush()
        except IntegrityError as exc:
            raced = await session.scalar(
                select(ToolCall).where(
                    ToolCall.connector_revision_id == context.revision.id,
                    ToolCall.external_tool_call_id == request.external_tool_call_id,
                )
            )
            if raced is None:
                raise
            if raced.arguments_sha256 != arguments_sha256 or raced.tool_name != request.tool_name:
                raise ToolCallIdempotencyConflictError(
                    "external Tool call id is already bound to different arguments"
                ) from exc
            return ToolCallResult(raced, replayed=True)

        now = self._now()
        if request.deadline_at <= now:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.CANCELLED,
                code="TOOL_DEADLINE_EXPIRED",
                detail="Tool deadline has expired",
                context=context,
            )

        try:
            await self._authorize_connector(session, context)
            route = await self._find_route(
                session,
                workspace_id=context.deployment.workspace_id,
                tool_name=request.tool_name,
            )
            if route is None:
                raise ToolExecutionDeniedError("TOOL_NOT_FOUND", "Tool is not available")
            allowlist = await self._connector_tool_allowlist(context.revision)
            if request.tool_name not in allowlist:
                raise ToolExecutionDeniedError(
                    "TOOL_CONNECTOR_ALLOWLIST_DENIED",
                    "Tool is not enabled for the connector",
                )
            call.target_deployment_id = route.deployment.id
            call.target_revision_id = route.revision.id
            call.target_activation_epoch = route.activation.activation_epoch
            if request.tool_schema_version != route.tool.schema_version:
                raise ToolExecutionDeniedError(
                    "TOOL_SCHEMA_MISMATCH", "Tool schema version is stale"
                )
            if not self._route_is_usable(route, context):
                raise ToolExecutionDeniedError(
                    "TOOL_GRANT_MISSING", "Tool is not granted to the active deployment"
                )
            self._validate_arguments(route.tool, arguments)
            if not await self._target_allowed(session, context, route):
                raise ToolExecutionDeniedError("TOOL_POLICY_DENIED", "Tool policy denied this call")
        except ToolExecutionDeniedError as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.DENIED,
                code=exc.code,
                detail=str(exc),
                context=context,
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError, ValidationError) as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.DENIED,
                code="TOOL_POLICY_INVALID",
                detail="Tool policy could not be evaluated",
                context=context,
                audit_detail={"error_type": type(exc).__name__},
            )
        except ToolInputValidationError as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.FAILED_FINAL,
                code="TOOL_INVALID_ARGUMENTS",
                detail=str(exc),
                context=context,
            )

        call.status = ToolCallStatus.AUTHORIZED
        await session.flush()
        call.status = ToolCallStatus.EXECUTING
        call.attempt_count += 1
        call.started_at = self._now()
        await session.flush()
        try:
            epoch, raw_result = await self._invoker.call(
                str(route.deployment.id),
                "invoke_tool",
                {
                    "tool_name": route.tool.name,
                    "arguments": arguments,
                    "context": self._plugin_context(context, route),
                },
            )
            if epoch != route.activation.activation_epoch:
                raise ToolStaleActivationError("Tool result belongs to a stale plugin activation")
            result = self._validate_result(route.tool, raw_result)
        except ToolStaleActivationError as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.CANCELLED,
                code="TOOL_STALE_ACTIVATION",
                detail=str(exc),
                context=context,
            )
        except (TimeoutError, PluginRuntimeError, ConnectionError) as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.FAILED_RETRYABLE,
                code="TOOL_RUNTIME_RETRYABLE",
                detail="Tool runtime is temporarily unavailable",
                context=context,
                audit_detail={"error_type": type(exc).__name__},
            )
        except ToolInputValidationError as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.FAILED_FINAL,
                code="TOOL_INVALID_RESULT",
                detail=str(exc),
                context=context,
            )
        except Exception as exc:
            return await self._finish(
                session,
                call,
                status=ToolCallStatus.FAILED_FINAL,
                code="TOOL_RUNTIME_FAILED",
                detail="Tool execution failed",
                context=context,
                audit_detail={"error_type": type(exc).__name__},
            )

        call.result = result
        call.status = ToolCallStatus.SUCCEEDED
        call.finished_at = self._now()
        call.error_code = None
        call.error_detail = None
        await self._audit(
            session,
            context=context,
            call=call,
            result="SUCCEEDED",
            detail={"plugin_id": route.manifest.plugin_id},
        )
        await session.flush()
        return ToolCallResult(call)

    async def get_call(
        self,
        session: AsyncSession,
        call_id: UUID,
        *,
        workspace_id: UUID,
    ) -> ToolCall:
        call = await session.scalar(
            select(ToolCall).where(ToolCall.id == call_id, ToolCall.workspace_id == workspace_id)
        )
        if call is None:
            raise ToolCallNotFoundError("Tool call not found")
        return call

    async def list_calls(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        limit: int,
        offset: int,
        status: ToolCallStatus | None = None,
        tool_name: str | None = None,
    ) -> tuple[list[ToolCall], int]:
        filters = [ToolCall.workspace_id == workspace_id]
        if status is not None:
            filters.append(ToolCall.status == status)
        if tool_name is not None and tool_name.strip():
            filters.append(ToolCall.tool_name == tool_name.strip())
        rows = list(
            await session.scalars(
                select(ToolCall)
                .where(*filters)
                .order_by(ToolCall.created_at.desc(), ToolCall.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        total = await session.scalar(select(func.count()).select_from(ToolCall).where(*filters))
        return rows, total or 0

    async def _resolve_connector_context(
        self,
        session: AsyncSession,
        *,
        deployment_revision_id: UUID,
        activation_epoch: int,
        connector_context_id: str,
    ) -> _ConnectorContext:
        row = (
            await session.execute(
                select(
                    PluginDeployment,
                    PluginDeploymentRevision,
                    PluginRevisionActivation,
                    Plugin,
                )
                .join(
                    PluginDeploymentRevision,
                    PluginDeploymentRevision.deployment_id == PluginDeployment.id,
                )
                .join(
                    PluginRevisionActivation,
                    PluginRevisionActivation.revision_id == PluginDeploymentRevision.id,
                )
                .join(Plugin, Plugin.id == PluginDeployment.plugin_id)
                .where(
                    PluginDeploymentRevision.id == deployment_revision_id,
                    PluginRevisionActivation.activation_epoch == activation_epoch,
                    PluginRevisionActivation.deployment_id == PluginDeployment.id,
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                    PluginDeployment.status == PluginDeploymentStatus.RUNNING,
                    PluginDeployment.active_revision_id == deployment_revision_id,
                    Plugin.plugin_id == MAIBOT_CONNECTOR_PLUGIN_ID,
                )
            )
        ).one_or_none()
        if row is None:
            raise ToolStaleActivationError("MaiBot connector activation is no longer current")
        deployment, revision, activation, _plugin = row

        try:
            claims = MaiBotConversationContextClaims.model_validate_json(
                self._cipher.decrypt(connector_context_id.encode("ascii"))
            )
        except (CredentialDecryptionError, UnicodeEncodeError, ValidationError) as exc:
            raise ToolExecutionDeniedError(
                "TOOL_CONTEXT_INVALID", "connector context is invalid"
            ) from exc

        source = await session.scalar(
            select(MaiBotBridgeEnvelope)
            .where(MaiBotBridgeEnvelope.id == claims.source_envelope_id)
            .with_for_update()
        )
        now = self._now()
        if source is None:
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_NOT_FOUND", "source message context was not found"
            )
        if (
            source.direction is not MaiBotBridgeDirection.TO_MAIBOT
            or source.kind is not MaiBotBridgeKind.MESSAGE
            or source.status not in {MaiBotBridgeStatus.SENT, MaiBotBridgeStatus.ACKED}
            or source.deployment_id != deployment.id
            or source.deployment_revision_id != revision.id
            or source.activation_id != activation.id
            or _as_utc(source.expires_at) <= now
            or source.bot_account_id is None
            or source.target_wxid is None
            or source.actor_principal_id is None
        ):
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source context is stale or undelivered"
            )

        try:
            authorization = OutboxAuthorizationContext.model_validate(source.authorization_context)
        except ValidationError as exc:
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source authorization is invalid"
            ) from exc
        if (
            authorization.workspace_id != deployment.workspace_id
            or authorization.deployment_id != deployment.id
            or authorization.deployment_revision_id != revision.id
            or authorization.actor_principal_id != source.actor_principal_id
            or authorization.chatroom_id != source.chatroom_id
            or authorization.contact_id != source.contact_id
        ):
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source authorization does not match"
            )

        account = await session.get(BotAccount, source.bot_account_id)
        principal = await session.get(Principal, source.actor_principal_id)
        if account is None or principal is None or not principal.active:
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source identity is unavailable"
            )
        connection = await session.get(GeweConnection, account.gewe_connection_id)
        event = (
            await session.get(NormalizedEvent, source.source_event_id)
            if source.source_event_id
            else None
        )
        inbox = await session.get(WebhookInbox, event.webhook_inbox_id) if event else None
        if (
            connection is None
            or connection.workspace_id != deployment.workspace_id
            or event is None
            or event.bot_account_id != account.id
            or event.actor_wxid != principal.external_id
            or event.conversation_id != source.target_wxid
            or inbox is None
            or inbox.trace_id != source.trace_id
            or inbox.app_id != account.app_id
        ):
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source message identity is invalid"
            )

        chatroom: Chatroom | None = None
        contact: Contact | None = None
        if source.chatroom_id is not None and source.contact_id is None:
            chatroom = await session.get(Chatroom, source.chatroom_id)
            active_member = await session.scalar(
                select(ChatroomMembership).where(
                    ChatroomMembership.chatroom_id == source.chatroom_id,
                    ChatroomMembership.member_wxid == principal.external_id,
                    ChatroomMembership.left_at.is_(None),
                )
            )
            if (
                chatroom is None
                or chatroom.bot_account_id != account.id
                or chatroom.chatroom_id != source.target_wxid
                or principal.principal_type is not PrincipalType.GROUP_MEMBER
                or active_member is None
            ):
                raise ToolExecutionDeniedError(
                    "TOOL_SOURCE_CONTEXT_INVALID", "group sender is not active"
                )
        elif source.contact_id is not None and source.chatroom_id is None:
            contact = await session.get(Contact, source.contact_id)
            if (
                contact is None
                or contact.bot_account_id != account.id
                or contact.external_id != source.target_wxid
                or not contact.active
                or principal.principal_type is not PrincipalType.CONTACT
            ):
                raise ToolExecutionDeniedError(
                    "TOOL_SOURCE_CONTEXT_INVALID", "private sender is invalid"
                )
        else:
            raise ToolExecutionDeniedError(
                "TOOL_SOURCE_CONTEXT_INVALID", "source conversation is ambiguous"
            )

        if not _scope_allows(
            revision.scope,
            workspace_id=deployment.workspace_id,
            account_id=account.id,
            chatroom_id=chatroom.id if chatroom is not None else None,
            contact_id=contact.id if contact is not None else None,
            conversation_id=source.target_wxid,
        ):
            raise ToolExecutionDeniedError(
                "TOOL_CONTEXT_SCOPE_MISMATCH", "connector scope does not include source"
            )
        return _ConnectorContext(
            deployment=deployment,
            revision=revision,
            activation=activation,
            source=source,
            account=account,
            chatroom=chatroom,
            contact=contact,
            principal=principal,
            authorization=authorization,
            context_digest=hashlib.sha256(connector_context_id.encode("utf-8")).hexdigest(),
        )

    async def _authorize_connector(
        self,
        session: AsyncSession,
        context: _ConnectorContext,
    ) -> None:
        if MAIBOT_FORWARD_CAPABILITY not in context.revision.grants:
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_GRANT_MISSING", "connector forwarding is not granted"
            )
        try:
            decision = await self._policy.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.deployment.workspace_id,
                    bot_account_id=context.account.id,
                    actor_principal_id=context.principal.id,
                    chatroom_id=context.chatroom.id if context.chatroom is not None else None,
                    contact_id=context.contact.id if context.contact is not None else None,
                    resource_type=context.authorization.resource_type,
                    resource_id=context.authorization.resource_id,
                    parent_plugin_id=context.authorization.parent_plugin_id,
                    trace_id=context.source.trace_id,
                ),
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError, ValidationError) as exc:
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_POLICY_INVALID", "connector policy is invalid"
            ) from exc
        if not decision.allowed:
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_POLICY_DENIED", "connector policy denied this call"
            )

    async def _target_allowed(
        self,
        session: AsyncSession,
        context: _ConnectorContext,
        route: _ToolRoute,
    ) -> bool:
        try:
            decision = await self._policy.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.deployment.workspace_id,
                    bot_account_id=context.account.id,
                    actor_principal_id=context.principal.id,
                    chatroom_id=context.chatroom.id if context.chatroom is not None else None,
                    contact_id=context.contact.id if context.contact is not None else None,
                    resource_type=AclResourceType.TOOL,
                    resource_id=route.tool.name,
                    parent_plugin_id=route.manifest.plugin_id,
                    trace_id=context.source.trace_id,
                ),
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError, ValidationError):
            return False
        return decision.allowed

    async def _tool_routes(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
    ) -> list[_ToolRoute]:
        rows = (
            await session.execute(
                select(
                    PluginDeployment,
                    Plugin,
                    PluginDeploymentRevision,
                    PluginPackageVersion,
                    PluginRevisionActivation,
                )
                .join(Plugin, Plugin.id == PluginDeployment.plugin_id)
                .join(
                    PluginDeploymentRevision,
                    PluginDeploymentRevision.id == PluginDeployment.active_revision_id,
                )
                .join(
                    PluginPackageVersion,
                    PluginPackageVersion.id == PluginDeploymentRevision.package_version_id,
                )
                .join(
                    PluginRevisionActivation,
                    (PluginRevisionActivation.revision_id == PluginDeploymentRevision.id)
                    & (PluginRevisionActivation.deployment_id == PluginDeployment.id),
                )
                .where(
                    PluginDeployment.workspace_id == workspace_id,
                    PluginDeployment.status == PluginDeploymentStatus.RUNNING,
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                    Plugin.retired_at.is_(None),
                    PluginPackageVersion.status.in_(
                        {PluginPackageStatus.AVAILABLE, PluginPackageStatus.VERIFIED}
                    ),
                )
                .order_by(PluginDeployment.created_at, PluginDeployment.id)
            )
        ).all()
        routes: list[_ToolRoute] = []
        for deployment, plugin, revision, package, activation in rows:
            try:
                manifest = PluginManifest.model_validate(package.manifest)
            except ValidationError:
                continue
            for tool in manifest.tools:
                routes.append(
                    _ToolRoute(deployment, plugin, revision, package, activation, manifest, tool)
                )
        return routes

    async def _find_route(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        tool_name: str,
    ) -> _ToolRoute | None:
        routes = await self._tool_routes(session, workspace_id=workspace_id)
        matches = [route for route in routes if route.tool.name == tool_name]
        if len(matches) > 1:
            raise ToolExecutionDeniedError(
                "TOOL_ROUTE_CONFLICT", "multiple active plugins provide this Tool"
            )
        return matches[0] if matches else None

    async def _connector_tool_allowlist(
        self,
        revision: PluginDeploymentRevision,
    ) -> frozenset[str]:
        try:
            raw = json.loads(self._cipher.decrypt(revision.config_ciphertext))
        except (CredentialDecryptionError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_CONFIG_INVALID", "connector configuration is unavailable"
            ) from exc
        if not isinstance(raw, Mapping):
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_CONFIG_INVALID", "connector configuration is invalid"
            )
        values = raw.get("tool_allowlist", [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ToolExecutionDeniedError(
                "TOOL_CONNECTOR_CONFIG_INVALID", "connector Tool allowlist is invalid"
            )
        return frozenset(item.strip() for item in values if item.strip())

    @staticmethod
    def _route_is_usable(route: _ToolRoute, context: _ConnectorContext) -> bool:
        return (
            route.tool.effect_class == "READ_ONLY"
            and route.activation.status is PluginActivationStatus.ACTIVE
            and route.deployment.status is PluginDeploymentStatus.RUNNING
            and _scope_allows(
                route.revision.scope,
                workspace_id=context.deployment.workspace_id,
                account_id=context.account.id,
                chatroom_id=context.chatroom.id if context.chatroom is not None else None,
                contact_id=context.contact.id if context.contact is not None else None,
                conversation_id=context.source.target_wxid or "",
            )
            and all(
                capability in route.revision.grants
                for capability in route.tool.required_capabilities
            )
        )

    def _validate_arguments(self, tool: PluginTool, arguments: dict[str, Any]) -> None:
        try:
            validator = Draft202012Validator(tool.input_schema or {"type": "object"})
            errors = sorted(validator.iter_errors(arguments), key=lambda error: list(error.path))
        except SchemaError as exc:
            raise ToolInputValidationError("Tool input schema is invalid") from exc
        if errors:
            raise ToolInputValidationError("Tool arguments do not match the declared schema")

    def _validate_result(self, tool: PluginTool, raw_result: Any) -> dict[str, Any]:
        if not isinstance(raw_result, dict):
            raise ToolInputValidationError("Tool result must be an object")
        try:
            encoded = json.dumps(
                raw_result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise ToolInputValidationError("Tool result is not valid JSON") from exc
        if len(encoded.encode("utf-8")) > self._max_result_bytes:
            raise ToolInputValidationError("Tool result exceeds the response limit")
        if tool.output_schema:
            try:
                validator = Draft202012Validator(tool.output_schema)
                errors = sorted(
                    validator.iter_errors(raw_result), key=lambda error: list(error.path)
                )
            except SchemaError as exc:
                raise ToolInputValidationError("Tool output schema is invalid") from exc
            if errors:
                raise ToolInputValidationError("Tool result does not match the declared schema")
        return cast(dict[str, Any], raw_result)

    def _canonical_arguments(self, arguments: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(arguments, dict):
            raise ToolInputValidationError("Tool arguments must be an object")
        try:
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ToolInputValidationError("Tool arguments are not valid JSON") from exc
        if len(encoded.encode("utf-8")) > self._max_argument_bytes:
            raise ToolInputValidationError("Tool arguments exceed the request limit")
        normalized = json.loads(encoded)
        if not isinstance(normalized, dict):
            raise ToolInputValidationError("Tool arguments must be an object")
        return cast(dict[str, Any], normalized), hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _plugin_context(context: _ConnectorContext, route: _ToolRoute) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "source": "maibot",
            "workspace_id": str(context.deployment.workspace_id),
            "bot_account_id": str(context.account.id),
            "chatroom_id": str(context.chatroom.id) if context.chatroom is not None else None,
            "contact_id": str(context.contact.id) if context.contact is not None else None,
            "actor_principal_id": str(context.principal.id),
            "actor_wxid": context.principal.external_id,
            "trace_id": str(context.source.trace_id),
            "tool_name": route.tool.name,
            "tool_plugin_id": route.manifest.plugin_id,
        }

    async def _record_rejected(
        self,
        session: AsyncSession,
        *,
        request: Any,
        context: _ConnectorContext,
        arguments: dict[str, Any],
        arguments_sha256: str,
        code: str,
        detail: str,
    ) -> ToolCallResult:
        call = ToolCall(
            workspace_id=context.deployment.workspace_id,
            connector_deployment_id=context.deployment.id,
            connector_revision_id=context.revision.id,
            connector_activation_id=context.activation.id,
            external_tool_call_id=request.external_tool_call_id,
            connector_context_digest=context.context_digest,
            tool_name=request.tool_name,
            tool_schema_version=request.tool_schema_version,
            invocation_mode=request.invocation_mode,
            arguments=arguments,
            arguments_sha256=arguments_sha256,
            trace_id=context.source.trace_id,
            actor_principal_id=context.principal.id,
            bot_account_id=context.account.id,
            chatroom_id=context.chatroom.id if context.chatroom is not None else None,
            contact_id=context.contact.id if context.contact is not None else None,
            status=ToolCallStatus.DENIED,
            error_code=code,
            error_detail=detail,
            deadline_at=request.deadline_at,
            available_at=self._now(),
            finished_at=self._now(),
        )
        session.add(call)
        await session.flush()
        await self._audit(
            session, context=context, call=call, result="DENIED", detail={"code": code}
        )
        await session.flush()
        return ToolCallResult(call)

    async def _finish(
        self,
        session: AsyncSession,
        call: ToolCall,
        *,
        status: ToolCallStatus,
        code: str,
        detail: str,
        context: _ConnectorContext,
        audit_detail: dict[str, Any] | None = None,
    ) -> ToolCallResult:
        call.status = status
        call.error_code = code[:100]
        call.error_detail = detail[:500]
        call.finished_at = self._now()
        await self._audit(
            session,
            context=context,
            call=call,
            result=status.value,
            detail={"code": code, **(audit_detail or {})},
        )
        await session.flush()
        return ToolCallResult(call)

    async def _audit(
        self,
        session: AsyncSession,
        *,
        context: _ConnectorContext,
        call: ToolCall,
        result: str,
        detail: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                workspace_id=context.deployment.workspace_id,
                trace_id=call.trace_id,
                actor_type="MAIBOT",
                actor_id=str(context.principal.id),
                action="tool.bridge.invoke",
                object_type="tool_call",
                object_id=str(call.id),
                result=result,
                detail={
                    "tool_name": call.tool_name,
                    "external_tool_call_id": call.external_tool_call_id,
                    "arguments_sha256": call.arguments_sha256,
                    **detail,
                },
            )
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _scope_allows(
    scope: Mapping[str, Any],
    *,
    workspace_id: UUID,
    account_id: UUID,
    chatroom_id: UUID | None,
    contact_id: UUID | None,
    conversation_id: str,
) -> bool:
    expected_workspace = scope.get("workspace_id")
    if expected_workspace is not None and str(expected_workspace) != str(workspace_id):
        return False
    checks = (
        ("bot_account_ids", str(account_id)),
        ("chatroom_ids", str(chatroom_id) if chatroom_id is not None else None),
        ("contact_ids", str(contact_id) if contact_id is not None else None),
        ("conversation_ids", conversation_id),
    )
    for key, actual in checks:
        configured = scope.get(key)
        if configured is None:
            continue
        if not isinstance(configured, list) or actual is None:
            return False
        if actual not in {str(item) for item in configured}:
            return False
    return True


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite's timezone-naive round-trip values before comparing."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
