from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    AuditEvent,
    BotAccount,
    Chatroom,
    ChatroomMembership,
    Contact,
    ConversationType,
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    WebhookInbox,
)
from wechat_bot.db.plugin_models import (
    Plugin,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginEventDispatch,
    PluginEventDispatchStatus,
    PluginPackageVersion,
)
from wechat_bot.db.policy_models import AclResourceType, Principal, PrincipalType
from wechat_bot.maibot.constants import MAIBOT_CONNECTOR_PLUGIN_ID
from wechat_bot.maibot.schemas import MaiBotEventSubmission
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.plugins.manifest import PluginCommand, PluginManifest
from wechat_bot.policy.schemas import AclEvaluationRequest, PrincipalCreate
from wechat_bot.policy.service import PolicyService

MAX_PLUGIN_ACTIONS = 20
MAX_REPLY_TEXT_LENGTH = 10_000
TERMINAL_PLUGIN_DISPATCH_STATUSES = frozenset(
    {
        PluginEventDispatchStatus.SUCCEEDED,
        PluginEventDispatchStatus.DENIED,
        PluginEventDispatchStatus.REJECTED,
    }
)


class EventDispatchError(RuntimeError):
    pass


class EventNotFoundError(EventDispatchError):
    pass


class PluginInvocationRetryableError(EventDispatchError):
    pass


class InvalidPluginActionError(EventDispatchError):
    pass


class PluginInvoker(Protocol):
    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]: ...


@dataclass(frozen=True, slots=True)
class TextActionSubmission:
    bot_account_id: UUID
    trace_id: UUID
    idempotency_key: str
    target_wxid: str
    text: str
    authorization_context: OutboxAuthorizationContext
    expires_at: datetime | None = None
    priority: int = 100


class TextActionSink(Protocol):
    async def submit_text(
        self,
        session: AsyncSession,
        submission: TextActionSubmission,
    ) -> None: ...


class MaiBotEventSink(Protocol):
    async def enqueue_event(
        self,
        session: AsyncSession,
        submission: MaiBotEventSubmission,
    ) -> object | None: ...


@dataclass(frozen=True, slots=True)
class DispatchResult:
    event_id: UUID
    considered_plugins: int
    invoked_plugins: int
    denied_plugins: int
    rejected_actions: int
    accepted_actions: int
    ignored: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedContext:
    workspace_id: UUID
    account: BotAccount
    chatroom: Chatroom | None
    contact: Contact | None
    principal: Principal | None
    message_text: str
    actor_nickname: str | None
    actor_cardname: str | None


@dataclass(frozen=True, slots=True)
class _PluginRoute:
    deployment: PluginDeployment
    plugin: Plugin
    revision: PluginDeploymentRevision
    manifest: PluginManifest


@dataclass(frozen=True, slots=True)
class _CommandRoute:
    route: _PluginRoute
    command: PluginCommand
    arguments: str


class EventDispatcher:
    def __init__(
        self,
        *,
        invoker: PluginInvoker,
        action_sink: TextActionSink,
        policy_service: PolicyService | None = None,
        maibot_sink: MaiBotEventSink | None = None,
    ) -> None:
        self._invoker = invoker
        self._action_sink = action_sink
        self._policy = policy_service or PolicyService()
        self._maibot_sink = maibot_sink

    async def dispatch(
        self,
        session: AsyncSession,
        event_id: UUID,
    ) -> DispatchResult:
        event = await session.get(NormalizedEvent, event_id)
        if event is None:
            raise EventNotFoundError("normalized event not found")
        inbox = await session.get(WebhookInbox, event.webhook_inbox_id)
        if inbox is None:
            raise EventNotFoundError("webhook inbox not found")
        if event.is_self or inbox.status is InboxStatus.IGNORED_SELF:
            return DispatchResult(event_id, 0, 0, 0, 0, 0, ignored=True)
        if inbox.status is InboxStatus.DISPATCHED:
            return DispatchResult(event_id, 0, 0, 0, 0, 0, ignored=True)
        if (
            event.conversation_type is ConversationType.SYSTEM
            or event.event_type == "gewe.callback_verification"
        ):
            inbox.status = InboxStatus.DISPATCHED
            inbox.error_code = None
            inbox.error_detail = None
            await session.flush()
            return DispatchResult(event_id, 0, 0, 0, 0, 0, ignored=True)

        context = await self._resolve_context(session, event)
        if context is None or event.conversation_id is None:
            inbox.status = InboxStatus.FAILED
            inbox.error_code = "EVENT_CONTEXT_UNRESOLVED"
            inbox.error_detail = "event has no active bot account or conversation"
            await session.flush()
            return DispatchResult(event_id, 0, 0, 0, 0, 0, ignored=True)

        routes = await self._plugin_routes(session, context)
        command_routes = self._command_routes(routes, context.message_text)
        if len(command_routes) > 1:
            inbox.status = InboxStatus.FAILED
            inbox.error_code = "PLUGIN_COMMAND_CONFLICT"
            inbox.error_detail = "multiple active deployments claim the same command"
            await self._audit(
                session,
                context=context,
                event=event,
                action="plugin.command.route",
                object_id=command_routes[0].command.name,
                result="REJECTED",
                detail={"matching_deployments": len(command_routes)},
            )
            await session.flush()
            return DispatchResult(event_id, len(routes), 0, 0, 0, 0)

        if command_routes:
            selected: list[tuple[_PluginRoute, PluginCommand | None, str]] = [
                (
                    command_routes[0].route,
                    command_routes[0].command,
                    command_routes[0].arguments,
                )
            ]
        elif _looks_like_command(context.message_text):
            selected = []
        else:
            selected = [
                (route, None, context.message_text)
                for route in routes
                if event.event_type in route.manifest.events
            ]

        invoked = 0
        denied = 0
        rejected_actions = 0
        accepted_actions = 0
        retryable_failures: list[str] = []
        for route, command, plugin_content in selected:
            dispatch_record = await self._dispatch_record(session, event, route)
            if dispatch_record.status in TERMINAL_PLUGIN_DISPATCH_STATUSES:
                continue
            dispatch_record.revision_id = route.revision.id
            resource_type, resource_id, parent_plugin_id = _resource(route, command)
            decision = await self._policy.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.workspace_id,
                    bot_account_id=context.account.id,
                    actor_principal_id=(
                        context.principal.id if context.principal is not None else None
                    ),
                    chatroom_id=(context.chatroom.id if context.chatroom is not None else None),
                    contact_id=(context.contact.id if context.contact is not None else None),
                    resource_type=resource_type,
                    resource_id=resource_id,
                    parent_plugin_id=parent_plugin_id,
                    trace_id=inbox.trace_id,
                ),
            )
            if not decision.allowed:
                denied += 1
                dispatch_record.status = PluginEventDispatchStatus.DENIED
                dispatch_record.completed_at = utc_now()
                dispatch_record.last_error_type = None
                await self._audit(
                    session,
                    context=context,
                    event=event,
                    action="plugin.event.dispatch",
                    object_id=str(route.deployment.id),
                    result="DENIED",
                    detail={
                        "plugin_id": route.manifest.plugin_id,
                        "resource_type": resource_type.value,
                        "resource_id": resource_id,
                        "policy_reason": decision.reason,
                    },
                )
                continue

            dispatch_record.attempt_count += 1
            dispatch_record.last_attempt_at = utc_now()
            dispatch_record.completed_at = None
            dispatch_record.last_error_type = None
            if route.manifest.plugin_id == MAIBOT_CONNECTOR_PLUGIN_ID:
                invoked += 1
                if event.actor_wxid is None:
                    rejected_actions += 1
                    dispatch_record.status = PluginEventDispatchStatus.REJECTED
                    dispatch_record.completed_at = utc_now()
                    await self._audit(
                        session,
                        context=context,
                        event=event,
                        action="plugin.event.dispatch",
                        object_id=str(route.deployment.id),
                        result="REJECTED",
                        detail={
                            "plugin_id": route.manifest.plugin_id,
                            "reason": "MaiBot bridge requires a resolved sender identity",
                        },
                    )
                    continue
                queued = None
                if self._maibot_sink is not None:
                    queued = await self._maibot_sink.enqueue_event(
                        session,
                        _maibot_submission(
                            event=event,
                            inbox=inbox,
                            context=context,
                            route=route,
                            authorization_context=_authorization_context(
                                context=context,
                                route=route,
                                command=command,
                            ),
                        ),
                    )
                if queued is None:
                    rejected_actions += 1
                    dispatch_record.status = PluginEventDispatchStatus.REJECTED
                else:
                    accepted_actions += 1
                    dispatch_record.status = PluginEventDispatchStatus.SUCCEEDED
                dispatch_record.accepted_action_count = int(queued is not None)
                dispatch_record.completed_at = utc_now()
                await self._audit(
                    session,
                    context=context,
                    event=event,
                    action="plugin.event.dispatch",
                    object_id=str(route.deployment.id),
                    result="QUEUED" if queued is not None else "REJECTED",
                    detail={
                        "plugin_id": route.manifest.plugin_id,
                        "accepted_actions": int(queued is not None),
                        "reason": (
                            None
                            if queued is not None
                            else "MaiBot bridge is unavailable or at capacity"
                        ),
                    },
                )
                continue

            try:
                _, result = await self._invoker.call(
                    str(route.deployment.id),
                    "handle_event",
                    {
                        "event": _event_envelope(
                            event,
                            inbox,
                            context,
                            content=plugin_content,
                            command=command,
                        )
                    },
                )
            except Exception as exc:
                retryable_failures.append(str(route.deployment.id))
                dispatch_record.status = PluginEventDispatchStatus.FAILED_RETRYABLE
                dispatch_record.last_error_type = type(exc).__name__
                await self._audit(
                    session,
                    context=context,
                    event=event,
                    action="plugin.event.dispatch",
                    object_id=str(route.deployment.id),
                    result="FAILED_RETRYABLE",
                    detail={
                        "plugin_id": route.manifest.plugin_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            invoked += 1
            try:
                actions = _plugin_actions(result)
                submissions = [
                    _text_submission(
                        action,
                        index=index,
                        event=event,
                        inbox=inbox,
                        context=context,
                        route=route,
                        command=command,
                    )
                    for index, action in enumerate(actions)
                ]
                if submissions and "message.reply.text" not in route.revision.grants:
                    raise InvalidPluginActionError(
                        "deployment revision lacks message.reply.text grant"
                    )
                for submission in submissions:
                    await self._action_sink.submit_text(session, submission)
                    accepted_actions += 1
            except InvalidPluginActionError as exc:
                rejected_actions += 1
                dispatch_record.status = PluginEventDispatchStatus.REJECTED
                dispatch_record.completed_at = utc_now()
                dispatch_record.last_error_type = type(exc).__name__
                await self._audit(
                    session,
                    context=context,
                    event=event,
                    action="plugin.action.submit",
                    object_id=str(route.deployment.id),
                    result="REJECTED",
                    detail={
                        "plugin_id": route.manifest.plugin_id,
                        "reason": str(exc),
                    },
                )
                continue

            dispatch_record.status = PluginEventDispatchStatus.SUCCEEDED
            dispatch_record.accepted_action_count = len(actions)
            dispatch_record.completed_at = utc_now()
            await self._audit(
                session,
                context=context,
                event=event,
                action="plugin.event.dispatch",
                object_id=str(route.deployment.id),
                result="SUCCEEDED",
                detail={
                    "plugin_id": route.manifest.plugin_id,
                    "accepted_actions": len(actions),
                },
            )

        if retryable_failures:
            inbox.status = InboxStatus.NORMALIZED
            inbox.error_code = "PLUGIN_DISPATCH_RETRYABLE"
            inbox.error_detail = f"{len(retryable_failures)} plugin deployment(s) require retry"
            await session.flush()
            raise PluginInvocationRetryableError(inbox.error_detail)

        inbox.status = InboxStatus.DISPATCHED
        inbox.error_code = None
        inbox.error_detail = None
        await session.flush()
        return DispatchResult(
            event_id=event.id,
            considered_plugins=len(routes),
            invoked_plugins=invoked,
            denied_plugins=denied,
            rejected_actions=rejected_actions,
            accepted_actions=accepted_actions,
        )

    @staticmethod
    async def _dispatch_record(
        session: AsyncSession,
        event: NormalizedEvent,
        route: _PluginRoute,
    ) -> PluginEventDispatch:
        record = await session.scalar(
            select(PluginEventDispatch).where(
                PluginEventDispatch.event_id == event.id,
                PluginEventDispatch.deployment_id == route.deployment.id,
            )
        )
        if record is not None:
            return record
        record = PluginEventDispatch(
            event_id=event.id,
            deployment_id=route.deployment.id,
            revision_id=route.revision.id,
            status=PluginEventDispatchStatus.PENDING,
            attempt_count=0,
            accepted_action_count=0,
        )
        session.add(record)
        await session.flush()
        return record

    async def _resolve_context(
        self,
        session: AsyncSession,
        event: NormalizedEvent,
    ) -> _ResolvedContext | None:
        if event.bot_account_id is None:
            return None
        row = (
            await session.execute(
                select(BotAccount, GeweConnection)
                .join(
                    GeweConnection,
                    BotAccount.gewe_connection_id == GeweConnection.id,
                )
                .where(BotAccount.id == event.bot_account_id)
            )
        ).one_or_none()
        if row is None:
            return None
        account, connection = row
        chatroom: Chatroom | None = None
        contact: Contact | None = None
        principal: Principal | None = None
        actor_nickname: str | None = None
        actor_cardname: str | None = None
        if event.conversation_type is ConversationType.GROUP and event.conversation_id:
            chatroom = await session.scalar(
                select(Chatroom).where(
                    Chatroom.bot_account_id == account.id,
                    Chatroom.chatroom_id == event.conversation_id,
                )
            )
            if chatroom is None:
                chatroom = Chatroom(
                    bot_account_id=account.id,
                    chatroom_id=event.conversation_id,
                    discovered_from="WEBHOOK",
                    placeholder=True,
                )
                session.add(chatroom)
                await session.flush()
            if event.actor_wxid:
                membership = await self._ensure_membership(session, chatroom.id, event.actor_wxid)
                actor_nickname = membership.nickname
                actor_cardname = membership.display_name
                principal = await self._policy.create_principal(
                    session,
                    PrincipalCreate(
                        workspace_id=connection.workspace_id,
                        principal_type=PrincipalType.GROUP_MEMBER,
                        external_id=event.actor_wxid,
                    ),
                )
        elif event.conversation_type is ConversationType.PRIVATE and event.conversation_id:
            external_id = event.actor_wxid or event.conversation_id
            contact = await session.scalar(
                select(Contact).where(
                    Contact.bot_account_id == account.id,
                    Contact.external_id == external_id,
                )
            )
            if contact is None:
                contact = Contact(
                    bot_account_id=account.id,
                    external_id=external_id,
                    contact_type="DISCOVERED",
                    active=True,
                )
                session.add(contact)
                await session.flush()
            principal = await self._policy.create_principal(
                session,
                PrincipalCreate(
                    workspace_id=connection.workspace_id,
                    principal_type=PrincipalType.CONTACT,
                    external_id=external_id,
                ),
            )
            actor_nickname = contact.nickname
            actor_cardname = contact.remark
        return _ResolvedContext(
            workspace_id=connection.workspace_id,
            account=account,
            chatroom=chatroom,
            contact=contact,
            principal=principal,
            message_text=_message_text(event),
            actor_nickname=actor_nickname,
            actor_cardname=actor_cardname,
        )

    @staticmethod
    async def _ensure_membership(
        session: AsyncSession,
        chatroom_id: UUID,
        actor_wxid: str,
    ) -> ChatroomMembership:
        active = await session.scalar(
            select(ChatroomMembership).where(
                ChatroomMembership.chatroom_id == chatroom_id,
                ChatroomMembership.member_wxid == actor_wxid,
                ChatroomMembership.left_at.is_(None),
            )
        )
        if active is not None:
            return active
        latest_epoch = await session.scalar(
            select(func.max(ChatroomMembership.membership_epoch)).where(
                ChatroomMembership.chatroom_id == chatroom_id,
                ChatroomMembership.member_wxid == actor_wxid,
            )
        )
        membership = ChatroomMembership(
            chatroom_id=chatroom_id,
            member_wxid=actor_wxid,
            membership_epoch=(latest_epoch or 0) + 1,
        )
        session.add(membership)
        await session.flush()
        return membership

    @staticmethod
    async def _plugin_routes(
        session: AsyncSession,
        context: _ResolvedContext,
    ) -> list[_PluginRoute]:
        statement = (
            select(
                PluginDeployment,
                Plugin,
                PluginDeploymentRevision,
                PluginPackageVersion,
            )
            .join(Plugin, PluginDeployment.plugin_id == Plugin.id)
            .join(
                PluginDeploymentRevision,
                PluginDeployment.active_revision_id == PluginDeploymentRevision.id,
            )
            .join(
                PluginPackageVersion,
                PluginDeploymentRevision.package_version_id == PluginPackageVersion.id,
            )
            .where(
                PluginDeployment.workspace_id == context.workspace_id,
                PluginDeployment.status == PluginDeploymentStatus.RUNNING,
            )
            .order_by(PluginDeployment.created_at, PluginDeployment.id)
        )
        routes: list[_PluginRoute] = []
        for deployment, plugin, revision, package in (await session.execute(statement)).all():
            manifest = PluginManifest.model_validate(package.manifest)
            if _scope_matches(revision.scope, context):
                routes.append(_PluginRoute(deployment, plugin, revision, manifest))
        return routes

    @staticmethod
    def _command_routes(
        routes: list[_PluginRoute],
        message_text: str,
    ) -> list[_CommandRoute]:
        parsed = _parse_command(message_text)
        if parsed is None:
            return []
        command_name, arguments = parsed
        matches: list[_CommandRoute] = []
        for route in routes:
            for command in route.manifest.commands:
                names = (command.name, *command.aliases)
                if any(command_name.casefold() == name.casefold() for name in names):
                    matches.append(_CommandRoute(route, command, arguments))
                    break
        return matches

    @staticmethod
    async def _audit(
        session: AsyncSession,
        *,
        context: _ResolvedContext,
        event: NormalizedEvent,
        action: str,
        object_id: str,
        result: str,
        detail: dict[str, Any],
    ) -> None:
        session.add(
            AuditEvent(
                workspace_id=context.workspace_id,
                trace_id=(
                    await session.scalar(
                        select(WebhookInbox.trace_id).where(
                            WebhookInbox.id == event.webhook_inbox_id
                        )
                    )
                ),
                actor_type=(
                    context.principal.principal_type.value
                    if context.principal is not None
                    else "UNKNOWN"
                ),
                actor_id=(
                    context.principal.external_id if context.principal is not None else "unknown"
                ),
                action=action,
                object_type="plugin_deployment",
                object_id=object_id,
                result=result,
                detail=detail,
            )
        )


def command_resource_id(plugin_id: str, command_name: str) -> str:
    return f"command.{plugin_id}.{command_name}"


def _resource(
    route: _PluginRoute,
    command: PluginCommand | None,
) -> tuple[AclResourceType, str, str | None]:
    if command is not None:
        return (
            AclResourceType.COMMAND,
            command_resource_id(route.manifest.plugin_id, command.name),
            route.manifest.plugin_id,
        )
    return AclResourceType.PLUGIN, route.manifest.plugin_id, None


def _message_text(event: NormalizedEvent) -> str:
    raw = event.content.get("raw_content", "")
    text = raw if isinstance(raw, str) else ""
    if event.conversation_type is ConversationType.GROUP and event.actor_wxid:
        prefix = f"{event.actor_wxid}:\n"
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _parse_command(message_text: str) -> tuple[str, str] | None:
    stripped = message_text.strip()
    if not stripped.startswith(("/", "!")):
        return None
    command_line = stripped[1:].lstrip()
    if not command_line:
        return None
    name, separator, arguments = command_line.partition(" ")
    return name, arguments.lstrip() if separator else ""


def _looks_like_command(message_text: str) -> bool:
    return _parse_command(message_text) is not None


def _scope_matches(scope: dict[str, Any], context: _ResolvedContext) -> bool:
    expected_workspace = scope.get("workspace_id")
    if expected_workspace is not None and str(expected_workspace) != str(context.workspace_id):
        return False
    filters: tuple[tuple[str, str | None], ...] = (
        ("bot_account_ids", str(context.account.id)),
        ("chatroom_ids", str(context.chatroom.id) if context.chatroom else None),
        ("contact_ids", str(context.contact.id) if context.contact else None),
        (
            "conversation_ids",
            context.chatroom.chatroom_id
            if context.chatroom
            else context.contact.external_id
            if context.contact
            else None,
        ),
    )
    for key, actual in filters:
        configured = scope.get(key)
        if configured is None:
            continue
        if not isinstance(configured, list) or actual is None:
            return False
        if actual not in {str(item) for item in configured}:
            return False
    return True


def _event_envelope(
    event: NormalizedEvent,
    inbox: WebhookInbox,
    context: _ResolvedContext,
    *,
    content: str,
    command: PluginCommand | None,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "event_id": str(event.id),
        "trace_id": str(inbox.trace_id),
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "received_at": event.created_at.isoformat(),
        "workspace_id": str(context.workspace_id),
        "bot": {
            "account_id": str(context.account.id),
            "app_id": context.account.app_id,
            "wxid": context.account.wxid,
        },
        "conversation": {
            "type": event.conversation_type.value,
            "external_id": event.conversation_id,
        },
        "actor": {"wxid": event.actor_wxid},
        "message": {
            "provider_message_id": event.provider_message_id,
            "is_self": event.is_self,
            "text": content,
        },
        "command": ({"name": command.name, "arguments": content} if command is not None else None),
        "content": content,
        "source": {"inbox_id": str(inbox.id), "raw_ref": event.raw_ref},
    }


def _plugin_actions(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise InvalidPluginActionError("plugin result must be an object")
    actions = result.get("actions", [])
    if not isinstance(actions, list):
        raise InvalidPluginActionError("plugin actions must be an array")
    if len(actions) > MAX_PLUGIN_ACTIONS:
        raise InvalidPluginActionError("plugin returned too many actions")
    if any(not isinstance(action, dict) for action in actions):
        raise InvalidPluginActionError("plugin action must be an object")
    return actions


def _text_submission(
    action: dict[str, Any],
    *,
    index: int,
    event: NormalizedEvent,
    inbox: WebhookInbox,
    context: _ResolvedContext,
    route: _PluginRoute,
    command: PluginCommand | None,
) -> TextActionSubmission:
    action_type = action.get("action_type") or action.get("type")
    if action_type not in {"message.reply.text", "reply.text"}:
        raise InvalidPluginActionError("unsupported plugin action type")
    content = action.get("content")
    if content is None and isinstance(action.get("payload"), dict):
        content = action["payload"].get("text")
    if not isinstance(content, str) or not content.strip():
        raise InvalidPluginActionError("reply text must be a non-empty string")
    if len(content) > MAX_REPLY_TEXT_LENGTH:
        raise InvalidPluginActionError("reply text exceeds the maximum length")
    action_key = action.get("action_key", str(index))
    if not isinstance(action_key, str) or not action_key or len(action_key) > 120:
        raise InvalidPluginActionError("action_key is invalid")
    if event.conversation_id is None:
        raise InvalidPluginActionError("reply event has no conversation target")
    authorization_context = _authorization_context(
        context=context,
        route=route,
        command=command,
    )
    return TextActionSubmission(
        bot_account_id=context.account.id,
        trace_id=inbox.trace_id,
        idempotency_key=(f"plugin:{route.deployment.id}:event:{event.id}:action:{action_key}"),
        target_wxid=event.conversation_id,
        text=content,
        authorization_context=authorization_context,
    )


def _authorization_context(
    *,
    context: _ResolvedContext,
    route: _PluginRoute,
    command: PluginCommand | None,
) -> OutboxAuthorizationContext:
    resource_type, resource_id, parent_plugin_id = _resource(route, command)
    return OutboxAuthorizationContext(
        workspace_id=context.workspace_id,
        deployment_id=route.deployment.id,
        deployment_revision_id=route.revision.id,
        actor_principal_id=(context.principal.id if context.principal is not None else None),
        chatroom_id=(context.chatroom.id if context.chatroom is not None else None),
        contact_id=(context.contact.id if context.contact is not None else None),
        resource_type=resource_type,
        resource_id=resource_id,
        parent_plugin_id=parent_plugin_id,
    )


def _maibot_submission(
    *,
    event: NormalizedEvent,
    inbox: WebhookInbox,
    context: _ResolvedContext,
    route: _PluginRoute,
    authorization_context: OutboxAuthorizationContext,
) -> MaiBotEventSubmission:
    if event.conversation_id is None or event.actor_wxid is None:
        raise EventDispatchError("MaiBot text event has no conversation actor")
    provider_id = event.provider_message_id or str(event.id)
    occurred_at = event.occurred_at or event.created_at
    return MaiBotEventSubmission(
        workspace_id=context.workspace_id,
        deployment_id=route.deployment.id,
        deployment_revision_id=route.revision.id,
        bot_account_id=context.account.id,
        bot_app_id=context.account.app_id,
        bot_wxid=context.account.wxid,
        trace_id=inbox.trace_id,
        event_id=event.id,
        event_type=event.event_type,
        conversation_type=event.conversation_type,
        conversation_external_id=event.conversation_id,
        actor_wxid=event.actor_wxid,
        actor_nickname=context.actor_nickname,
        actor_cardname=context.actor_cardname,
        group_name=context.chatroom.name if context.chatroom is not None else None,
        business_message_id=f"gewe:{context.account.app_id}:{provider_id}",
        occurred_at=occurred_at,
        text=context.message_text,
        authorization_context=authorization_context,
    )
