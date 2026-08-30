from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from wechat_bot.db.models import (
    AuditEvent,
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ChatroomMembership,
    ConversationType,
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    WebhookInbox,
    Workspace,
)
from wechat_bot.db.plugin_models import (
    Plugin,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginEventDispatch,
    PluginEventDispatchStatus,
    PluginPackageStatus,
    PluginPackageVersion,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    PolicyDecision,
    Principal,
    PrincipalType,
)
from wechat_bot.events.dispatcher import (
    EventDispatcher,
    TextActionSubmission,
    command_resource_id,
)
from wechat_bot.events.worker import EventDispatcherWorker
from wechat_bot.policy.schemas import AclRuleCreate
from wechat_bot.policy.service import PolicyService


@dataclass(slots=True)
class FakeInvoker:
    result: Any
    calls: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        self.calls.append((deployment_id, method, params))
        return 1, self.result


@dataclass(slots=True)
class FakeActionSink:
    submissions: list[TextActionSubmission] = field(default_factory=list)

    async def submit_text(
        self,
        session: AsyncSession,
        submission: TextActionSubmission,
    ) -> None:
        del session
        self.submissions.append(submission)


class FailingInvoker:
    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        del deployment_id, method, params
        raise RuntimeError("temporary plugin outage")


@dataclass(slots=True)
class FailDeploymentOnceInvoker:
    failing_deployment_id: str
    calls: list[str] = field(default_factory=list)
    failed: bool = False

    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        del method, params
        self.calls.append(deployment_id)
        if deployment_id == self.failing_deployment_id and not self.failed:
            self.failed = True
            raise RuntimeError("temporary second plugin outage")
        return 1, {"actions": []}


@dataclass(frozen=True, slots=True)
class DispatchSeed:
    workspace_id: UUID
    account_id: UUID
    event_id: UUID
    inbox_id: UUID
    deployment_id: UUID
    group_external_id: str


async def test_authorized_command_dispatches_plugin_and_locks_reply_target(
    app: FastAPI,
    client: object,
) -> None:
    del client
    invoker = FakeInvoker(
        {
            "actions": [
                {
                    "type": "reply.text",
                    "action_key": "echo-reply",
                    "content": "hello back",
                    "target": {"conversation_id": "attacker-controlled-target"},
                }
            ]
        }
    )
    sink = FakeActionSink()
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=True)
        dispatcher = EventDispatcher(invoker=invoker, action_sink=sink)

        result = await dispatcher.dispatch(session, seed.event_id)
        await session.commit()

        inbox = await session.get(WebhookInbox, seed.inbox_id)
        chatroom = await session.scalar(
            select(Chatroom).where(Chatroom.chatroom_id == seed.group_external_id)
        )
        membership_count = await session.scalar(
            select(func.count()).select_from(ChatroomMembership)
        )
        principal = await session.scalar(
            select(Principal).where(
                Principal.principal_type == PrincipalType.GROUP_MEMBER,
                Principal.external_id == "wxid_member",
            )
        )
        decision_count = await session.scalar(select(func.count()).select_from(PolicyDecision))

    assert result.invoked_plugins == 1
    assert result.accepted_actions == 1
    assert result.denied_plugins == 0
    assert inbox is not None and inbox.status is InboxStatus.DISPATCHED
    assert chatroom is not None and chatroom.placeholder is True
    assert chatroom.discovered_from == "WEBHOOK"
    assert membership_count == 1
    assert principal is not None
    assert decision_count == 1
    assert len(invoker.calls) == 1
    event_payload = invoker.calls[0][2]
    assert event_payload is not None
    assert event_payload["event"]["content"] == "hello"
    assert event_payload["event"]["command"] == {
        "name": "echo",
        "arguments": "hello",
    }
    assert len(sink.submissions) == 1
    assert sink.submissions[0].target_wxid == seed.group_external_id
    assert sink.submissions[0].text == "hello back"
    assert sink.submissions[0].idempotency_key.endswith(":action:echo-reply")
    authorization = sink.submissions[0].authorization_context
    assert authorization.deployment_id == seed.deployment_id
    assert authorization.deployment_revision_id is not None
    assert authorization.actor_principal_id is not None
    assert authorization.chatroom_id is not None
    assert authorization.resource_type is AclResourceType.COMMAND
    assert authorization.resource_id == command_resource_id("builtin.echo", "echo")
    assert authorization.parent_plugin_id == "builtin.echo"


async def test_default_deny_never_invokes_plugin(
    app: FastAPI,
    client: object,
) -> None:
    del client
    invoker = FakeInvoker({"actions": []})
    sink = FakeActionSink()
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=False)
        dispatcher = EventDispatcher(invoker=invoker, action_sink=sink)

        result = await dispatcher.dispatch(session, seed.event_id)
        await session.commit()

        inbox = await session.get(WebhookInbox, seed.inbox_id)
        decisions = list(await session.scalars(select(PolicyDecision)))

    assert result.denied_plugins == 1
    assert result.invoked_plugins == 0
    assert invoker.calls == []
    assert sink.submissions == []
    assert inbox is not None and inbox.status is InboxStatus.DISPATCHED
    assert len(decisions) == 1
    assert decisions[0].effect is AclEffect.DENY


async def test_unknown_plugin_action_is_rejected_and_audited(
    app: FastAPI,
    client: object,
) -> None:
    del client
    invoker = FakeInvoker(
        {
            "actions": [
                {
                    "type": "message.send.arbitrary",
                    "content": "must not be sent",
                }
            ]
        }
    )
    sink = FakeActionSink()
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=True)
        dispatcher = EventDispatcher(invoker=invoker, action_sink=sink)

        result = await dispatcher.dispatch(session, seed.event_id)
        await session.commit()

        inbox = await session.get(WebhookInbox, seed.inbox_id)
        rejected_audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "plugin.action.submit",
                AuditEvent.result == "REJECTED",
            )
        )

    assert result.invoked_plugins == 1
    assert result.rejected_actions == 1
    assert result.accepted_actions == 0
    assert sink.submissions == []
    assert inbox is not None and inbox.status is InboxStatus.DISPATCHED
    assert rejected_audit is not None
    assert rejected_audit.detail["reason"] == "unsupported plugin action type"


async def test_worker_consumes_normalized_event_outside_webhook_request(
    app: FastAPI,
    client: object,
) -> None:
    del client
    invoker = FakeInvoker({"actions": []})
    sink = FakeActionSink()
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=True)
        await session.commit()

    worker = EventDispatcherWorker(
        database=database,
        dispatcher=EventDispatcher(invoker=invoker, action_sink=sink),
    )
    attempted = await worker.run_once()

    async with database.session_factory() as session:
        inbox = await session.get(WebhookInbox, seed.inbox_id)
    assert attempted == 1
    assert inbox is not None and inbox.status is InboxStatus.DISPATCHED
    assert len(invoker.calls) == 1


async def test_worker_persists_retry_attempts_and_stops_at_limit(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=True)
        await session.commit()

    worker = EventDispatcherWorker(
        database=database,
        dispatcher=EventDispatcher(
            invoker=FailingInvoker(),
            action_sink=FakeActionSink(),
        ),
        poll_interval_seconds=0.001,
        max_retry_delay_seconds=0.001,
        max_attempts=2,
    )
    assert await worker.run_once() == 1
    await asyncio.sleep(0.01)
    assert await worker.run_once() == 1

    async with database.session_factory() as session:
        inbox = await session.get(WebhookInbox, seed.inbox_id)
    assert inbox is not None
    assert inbox.status is InboxStatus.FAILED
    assert inbox.dispatch_attempt_count == 2
    assert inbox.error_code == "PLUGIN_DISPATCH_RETRIES_EXHAUSTED"


async def test_retry_skips_plugins_with_terminal_dispatch_records(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    async with database.session_factory() as session:
        seed = await _seed(session, grant_command=False)
        event = await session.get(NormalizedEvent, seed.event_id)
        assert event is not None
        event.content = {
            "msg_type": "1",
            "raw_content": "wxid_member:\nhello plugins",
        }
        second_deployment_id = await _add_event_plugin(
            session,
            workspace_id=seed.workspace_id,
            plugin_id="test.second",
            name="Second",
        )
        for plugin_id in ("builtin.echo", "test.second"):
            await PolicyService().create_rule(
                session,
                AclRuleCreate(
                    workspace_id=seed.workspace_id,
                    scope_type=AclScopeType.BOT_ACCOUNT,
                    scope_id=str(seed.account_id),
                    resource_type=AclResourceType.PLUGIN,
                    resource_id=plugin_id,
                    effect=AclEffect.ALLOW,
                    reason="allow event plugin for retry ledger test",
                ),
                created_by="test",
            )
        await session.commit()

    invoker = FailDeploymentOnceInvoker(str(second_deployment_id))
    worker = EventDispatcherWorker(
        database=database,
        dispatcher=EventDispatcher(invoker=invoker, action_sink=FakeActionSink()),
        poll_interval_seconds=0.001,
        max_retry_delay_seconds=0.001,
    )
    assert await worker.run_once() == 1
    await asyncio.sleep(0.01)
    assert await worker.run_once() == 1

    async with database.session_factory() as session:
        inbox = await session.get(WebhookInbox, seed.inbox_id)
        records = list(
            await session.scalars(
                select(PluginEventDispatch).where(PluginEventDispatch.event_id == seed.event_id)
            )
        )

    assert inbox is not None and inbox.status is InboxStatus.DISPATCHED
    assert invoker.calls.count(str(seed.deployment_id)) == 1
    assert invoker.calls.count(str(second_deployment_id)) == 2
    assert len(records) == 2
    records_by_deployment = {record.deployment_id: record for record in records}
    assert records_by_deployment[seed.deployment_id].status is (PluginEventDispatchStatus.SUCCEEDED)
    assert records_by_deployment[seed.deployment_id].attempt_count == 1
    assert records_by_deployment[second_deployment_id].status is (
        PluginEventDispatchStatus.SUCCEEDED
    )
    assert records_by_deployment[second_deployment_id].attempt_count == 2


async def _seed(session: AsyncSession, *, grant_command: bool) -> DispatchSeed:
    workspace = Workspace(name="Dispatch", slug=f"dispatch-{uuid7()}")
    session.add(workspace)
    await session.flush()
    connection = GeweConnection(
        workspace_id=workspace.id,
        name="Primary",
        api_base_url="https://api.gewe.test",
        token_ciphertext=b"encrypted",
        token_fingerprint="0123456789abcdef",
        callback_secret_ciphertext=b"encrypted",
        callback_secret_hash=uuid7().hex + uuid7().hex,
    )
    session.add(connection)
    await session.flush()
    account = BotAccount(
        gewe_connection_id=connection.id,
        app_id="app-dispatch",
        wxid="wxid_bot",
        status=BotAccountStatus.ONLINE,
    )
    session.add(account)
    await session.flush()

    trace_id = uuid7()
    inbox = WebhookInbox(
        gewe_connection_id=connection.id,
        app_id=account.app_id,
        new_msg_id="9007199254740993",
        dedup_key=f"message:{uuid7()}",
        payload_sha256="a" * 64,
        schema_version="v2",
        raw_payload={"redacted": True},
        trace_id=trace_id,
        status=InboxStatus.NORMALIZED,
    )
    session.add(inbox)
    await session.flush()
    group_external_id = "12345678901234567890@chatroom"
    event = NormalizedEvent(
        webhook_inbox_id=inbox.id,
        bot_account_id=account.id,
        event_type="gewe.message.text",
        conversation_type=ConversationType.GROUP,
        conversation_id=group_external_id,
        actor_wxid="wxid_member",
        to_wxid="wxid_bot",
        provider_message_id=inbox.new_msg_id,
        is_self=False,
        content={"msg_type": "1", "raw_content": "wxid_member:\n/echo hello"},
        raw_ref=f"db:webhook_inbox/{inbox.id}",
    )
    session.add(event)

    plugin = Plugin(
        workspace_id=workspace.id,
        plugin_id="builtin.echo",
        name="Echo",
        description="test",
    )
    session.add(plugin)
    await session.flush()
    manifest = {
        "id": "builtin.echo",
        "name": "Echo",
        "version": "0.1.0",
        "core_api": "1",
        "entrypoint": "plugin:create_plugin",
        "events": ["gewe.message.text"],
        "commands": [{"name": "echo", "aliases": [], "description": ""}],
        "tools": [],
        "capabilities": ["message.reply.text"],
        "timeout_seconds": 5,
        "config_schema": {},
    }
    package = PluginPackageVersion(
        plugin_id=plugin.id,
        semantic_version="0.1.0",
        package_sha256=uuid7().hex + uuid7().hex,
        manifest=manifest,
        package_path="unused-in-dispatch-test",
        status=PluginPackageStatus.AVAILABLE,
    )
    session.add(package)
    await session.flush()
    deployment = PluginDeployment(
        workspace_id=workspace.id,
        plugin_id=plugin.id,
        name="Echo",
        status=PluginDeploymentStatus.RUNNING,
    )
    session.add(deployment)
    await session.flush()
    revision = PluginDeploymentRevision(
        deployment_id=deployment.id,
        package_version_id=package.id,
        revision_number=1,
        config_ciphertext=b"encrypted",
        config_fingerprint="b" * 64,
        scope={"workspace_id": str(workspace.id)},
        grants=["message.reply.text"],
        content_sha256="c" * 64,
    )
    session.add(revision)
    await session.flush()
    deployment.active_revision_id = revision.id

    if grant_command:
        await PolicyService().create_rule(
            session,
            AclRuleCreate(
                workspace_id=workspace.id,
                scope_type=AclScopeType.BOT_ACCOUNT,
                scope_id=str(account.id),
                resource_type=AclResourceType.COMMAND,
                resource_id=command_resource_id("builtin.echo", "echo"),
                effect=AclEffect.ALLOW,
                reason="allow echo command for dispatch test",
            ),
            created_by="test",
        )
    await session.flush()
    return DispatchSeed(
        workspace_id=workspace.id,
        account_id=account.id,
        event_id=event.id,
        inbox_id=inbox.id,
        deployment_id=deployment.id,
        group_external_id=group_external_id,
    )


async def _add_event_plugin(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    plugin_id: str,
    name: str,
) -> UUID:
    plugin = Plugin(
        workspace_id=workspace_id,
        plugin_id=plugin_id,
        name=name,
        description="test event plugin",
    )
    session.add(plugin)
    await session.flush()
    package = PluginPackageVersion(
        plugin_id=plugin.id,
        semantic_version="0.1.0",
        package_sha256=uuid7().hex + uuid7().hex,
        manifest={
            "id": plugin_id,
            "name": name,
            "version": "0.1.0",
            "core_api": "1",
            "entrypoint": "plugin:create_plugin",
            "events": ["gewe.message.text"],
            "commands": [],
            "tools": [],
            "capabilities": [],
            "timeout_seconds": 5,
            "config_schema": {},
        },
        package_path="unused-in-dispatch-test",
        status=PluginPackageStatus.AVAILABLE,
    )
    session.add(package)
    await session.flush()
    deployment = PluginDeployment(
        workspace_id=workspace_id,
        plugin_id=plugin.id,
        name=name,
        status=PluginDeploymentStatus.RUNNING,
    )
    session.add(deployment)
    await session.flush()
    revision = PluginDeploymentRevision(
        deployment_id=deployment.id,
        package_version_id=package.id,
        revision_number=1,
        config_ciphertext=b"encrypted",
        config_fingerprint="d" * 64,
        scope={"workspace_id": str(workspace_id)},
        grants=[],
        content_sha256="e" * 64,
    )
    session.add(revision)
    await session.flush()
    deployment.active_revision_id = revision.id
    await session.flush()
    return deployment.id
