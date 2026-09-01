from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import utc_now
from wechat_bot.db.maibot_models import (
    MaiBotBridgeDirection,
    MaiBotBridgeEnvelope,
    MaiBotBridgeKind,
    MaiBotBridgeStatus,
)
from wechat_bot.db.models import (
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
    PluginActivationStatus,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginPackageStatus,
    PluginPackageVersion,
    PluginRevisionActivation,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    Principal,
    PrincipalType,
)
from wechat_bot.db.tool_models import ToolCall, ToolCallStatus, ToolInvocationMode
from wechat_bot.maibot.constants import MAIBOT_CONNECTOR_PLUGIN_ID, MAIBOT_FORWARD_CAPABILITY
from wechat_bot.maibot.schemas import MaiBotConversationContextClaims
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.plugins.supervisor import PluginRuntimeError
from wechat_bot.policy.schemas import AclRuleCreate
from wechat_bot.policy.service import PolicyService
from wechat_bot.tool_bridge.schemas import ToolCallRequest
from wechat_bot.tool_bridge.service import (
    ToolBrokerService,
    ToolCallIdempotencyConflictError,
    ToolExecutionDeniedError,
    ToolStaleActivationError,
)


class RecordingInvoker:
    def __init__(
        self, result: Any = None, *, epoch_delta: int = 0, error: BaseException | None = None
    ) -> None:
        self.result = result if result is not None else {"ok": True, "city": "北京"}
        self.epoch_delta = epoch_delta
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        self.calls.append((deployment_id, method, params))
        if self.error is not None:
            raise self.error
        return 1 + self.epoch_delta, self.result


@dataclass(frozen=True, slots=True)
class BridgeSeed:
    workspace_id: UUID
    account_id: UUID
    chatroom_id: UUID
    member_principal_id: UUID
    connector_deployment_id: UUID
    connector_revision_id: UUID
    connector_activation_id: UUID
    target_deployment_id: UUID
    target_revision_id: UUID
    target_activation_id: UUID
    target_tool_name: str
    context_id: str
    source_id: UUID


async def test_allowed_member_can_invoke_read_only_tool(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    invoker = RecordingInvoker()
    async with app.state.database.session_factory() as session:
        seed = await _seed(session, cipher=cipher)
        await session.commit()
        request = _request(seed)
        result = await ToolBrokerService(cipher, invoker).invoke(session, request=request)
        await session.commit()

        call = await session.get(ToolCall, result.call.id)
        assert call is not None
        assert call.status is ToolCallStatus.SUCCEEDED
        assert call.result == {"ok": True, "city": "北京"}
        assert len(invoker.calls) == 1
        audit = await session.scalar(select(ToolCall).where(ToolCall.id == result.call.id))
        assert audit is not None


async def test_member_acl_denial_does_not_reach_plugin(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    invoker = RecordingInvoker()
    async with app.state.database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_target=False)
        result = await ToolBrokerService(cipher, invoker).invoke(session, request=_request(seed))
        await session.commit()

    assert result.call.status is ToolCallStatus.DENIED
    assert result.call.error_code == "TOOL_POLICY_DENIED"
    assert invoker.calls == []


async def test_connector_allowlist_is_enforced_at_invoke_time(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    invoker = RecordingInvoker()
    async with app.state.database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, connector_allowlist=[])
        result = await ToolBrokerService(cipher, invoker).invoke(session, request=_request(seed))
        await session.commit()

    assert result.call.status is ToolCallStatus.DENIED
    assert result.call.error_code == "TOOL_CONNECTOR_ALLOWLIST_DENIED"
    assert invoker.calls == []


@pytest.mark.parametrize(
    ("effect_class", "target_grants", "expected_code"),
    [
        ("WRITE", ["network.http.get"], "TOOL_GRANT_MISSING"),
        ("READ_ONLY", [], "TOOL_GRANT_MISSING"),
    ],
)
async def test_non_read_only_or_missing_capability_is_denied(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
    effect_class: str,
    target_grants: list[str],
    expected_code: str,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    invoker = RecordingInvoker()
    async with app.state.database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            effect_class=effect_class,
            target_grants=target_grants,
        )
        result = await ToolBrokerService(cipher, invoker).invoke(session, request=_request(seed))
        await session.commit()

    assert result.call.status is ToolCallStatus.DENIED
    assert result.call.error_code == expected_code
    assert invoker.calls == []


async def test_forged_context_and_stale_activation_fail_closed(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    async with app.state.database.session_factory() as session:
        seed = await _seed(session, cipher=cipher)
        with pytest.raises(ToolExecutionDeniedError) as forged:
            await ToolBrokerService(cipher, RecordingInvoker()).invoke(
                session,
                request=_request(seed, context_id="not-a-valid-context"),
            )
            assert forged.value.code == "TOOL_CONTEXT_INVALID"
        with pytest.raises(ToolStaleActivationError):
            await ToolBrokerService(cipher, RecordingInvoker()).invoke(
                session,
                request=_request(seed, activation_epoch=2),
            )


async def test_idempotency_replays_and_conflicts_for_normal_and_autonomous_calls(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    invoker = RecordingInvoker()
    async with app.state.database.session_factory() as session:
        seed = await _seed(session, cipher=cipher)
        broker = ToolBrokerService(cipher, invoker)
        first = await broker.invoke(session, request=_request(seed, external_id="same"))
        replay = await broker.invoke(session, request=_request(seed, external_id="same"))
        assert replay.replayed is True
        assert replay.call.id == first.call.id
        with pytest.raises(ToolCallIdempotencyConflictError):
            await broker.invoke(
                session,
                request=_request(seed, external_id="same", arguments={"city": "上海"}),
            )

        autonomous = await broker.invoke(
            session,
            request=_request(
                seed,
                external_id="autonomous",
                invocation_mode=ToolInvocationMode.AUTONOMOUS,
            ),
        )
        autonomous_replay = await broker.invoke(
            session,
            request=_request(
                seed,
                external_id="autonomous",
                invocation_mode=ToolInvocationMode.AUTONOMOUS,
            ),
        )
        await session.commit()

    assert autonomous.call.status is ToolCallStatus.DENIED
    assert autonomous.call.error_code == "TOOL_AUTONOMOUS_DISABLED"
    assert autonomous_replay.replayed is True
    assert len(invoker.calls) == 1


async def test_stale_result_and_runtime_failure_are_recorded(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    async with app.state.database.session_factory() as session:
        stale_seed = await _seed(session, cipher=cipher)
        stale = await ToolBrokerService(cipher, RecordingInvoker(epoch_delta=1)).invoke(
            session, request=_request(stale_seed, external_id="stale")
        )
        failed = await ToolBrokerService(
            cipher, RecordingInvoker(error=PluginRuntimeError("runner unavailable"))
        ).invoke(session, request=_request(stale_seed, external_id="failed"))
        await session.commit()

    assert stale.call.status is ToolCallStatus.CANCELLED
    assert stale.call.error_code == "TOOL_STALE_ACTIVATION"
    assert failed.call.status is ToolCallStatus.FAILED_RETRYABLE
    assert failed.call.error_code == "TOOL_RUNTIME_RETRYABLE"


async def test_argument_schema_and_expired_deadline_are_persisted(
    app: FastAPI,
    client: AsyncClient,
    settings: Any,
) -> None:
    del client
    cipher = CredentialCipher.from_settings(settings)
    async with app.state.database.session_factory() as session:
        schema_seed = await _seed(session, cipher=cipher)
        invalid = await ToolBrokerService(cipher, RecordingInvoker()).invoke(
            session,
            request=_request(schema_seed, arguments={"unknown": "x"}),
        )
        expired = await ToolBrokerService(cipher, RecordingInvoker()).invoke(
            session,
            request=_request(
                schema_seed,
                external_id="expired",
                deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            ),
        )
        await session.commit()

    assert invalid.call.status is ToolCallStatus.FAILED_FINAL
    assert invalid.call.error_code == "TOOL_INVALID_ARGUMENTS"
    assert expired.call.status is ToolCallStatus.CANCELLED
    assert expired.call.error_code == "TOOL_DEADLINE_EXPIRED"


def _request(
    seed: BridgeSeed,
    *,
    external_id: str = "tool-call-1",
    arguments: dict[str, Any] | None = None,
    invocation_mode: ToolInvocationMode = ToolInvocationMode.USER_REQUESTED,
    activation_epoch: int = 1,
    context_id: str | None = None,
    deadline_at: datetime | None = None,
) -> ToolCallRequest:
    return ToolCallRequest(
        external_tool_call_id=external_id,
        connector_context_id=context_id or seed.context_id,
        deployment_revision_id=seed.connector_revision_id,
        activation_epoch=activation_epoch,
        tool_name=seed.target_tool_name,
        tool_schema_version="1.0",
        arguments=arguments or {"city": "北京"},
        invocation_mode=invocation_mode,
        deadline_at=deadline_at or (datetime.now(UTC) + timedelta(minutes=1)),
    )


async def _seed(
    session: AsyncSession,
    *,
    cipher: CredentialCipher,
    allow_target: bool = True,
    connector_allowlist: list[str] | None = None,
    effect_class: str = "READ_ONLY",
    target_grants: list[str] | None = None,
) -> BridgeSeed:
    suffix = uuid4().hex
    workspace = Workspace(name="Tool Bridge", slug=f"tool-bridge-{suffix}")
    session.add(workspace)
    await session.flush()
    connection = GeweConnection(
        workspace_id=workspace.id,
        name="Primary",
        api_base_url="https://api.gewe.test",
        token_ciphertext=b"encrypted-token",
        token_fingerprint="0123456789abcdef",
        callback_secret_ciphertext=b"encrypted-callback",
        callback_secret_hash=hashlib.sha256(suffix.encode()).hexdigest(),
    )
    session.add(connection)
    await session.flush()
    account = BotAccount(
        gewe_connection_id=connection.id,
        app_id=f"app-{suffix}",
        wxid="wxid-bot",
        status=BotAccountStatus.ONLINE,
    )
    session.add(account)
    await session.flush()
    chatroom = Chatroom(
        bot_account_id=account.id,
        chatroom_id=f"{suffix}@chatroom",
        name="Tool Group",
        discovered_from="TEST",
        placeholder=False,
    )
    membership = ChatroomMembership(
        chatroom_id=chatroom.id,
        member_wxid="wxid-member",
        membership_epoch=1,
    )
    principal = Principal(
        workspace_id=workspace.id,
        principal_type=PrincipalType.GROUP_MEMBER,
        external_id="wxid-member",
        active=True,
    )
    session.add_all([chatroom, principal])
    await session.flush()
    membership.chatroom_id = chatroom.id
    session.add(membership)
    await session.flush()
    inbox = WebhookInbox(
        gewe_connection_id=connection.id,
        app_id=account.app_id,
        new_msg_id=f"msg-{suffix}",
        dedup_key=f"message:{suffix}",
        payload_sha256="a" * 64,
        schema_version="v2",
        raw_payload={"type": "message"},
        trace_id=uuid4(),
        status=InboxStatus.NORMALIZED,
    )
    session.add(inbox)
    await session.flush()
    event = NormalizedEvent(
        webhook_inbox_id=inbox.id,
        bot_account_id=account.id,
        event_type="gewe.message.text",
        conversation_type=ConversationType.GROUP,
        conversation_id=chatroom.chatroom_id,
        actor_wxid=principal.external_id,
        to_wxid=account.wxid,
        provider_message_id=inbox.new_msg_id,
        occurred_at=utc_now(),
        content={"raw_content": "天气"},
        raw_ref=f"db:webhook_inbox/{inbox.id}",
    )
    session.add(event)
    await session.flush()

    connector_plugin = Plugin(
        workspace_id=workspace.id,
        plugin_id=MAIBOT_CONNECTOR_PLUGIN_ID,
        name="MaiBot Connector",
    )
    session.add(connector_plugin)
    await session.flush()
    connector_package = PluginPackageVersion(
        plugin_id=connector_plugin.id,
        semantic_version="0.1.0",
        package_sha256=hashlib.sha256(f"connector:{suffix}".encode()).hexdigest(),
        manifest=_connector_manifest(),
        package_path="unused",
        status=PluginPackageStatus.AVAILABLE,
    )
    session.add(connector_package)
    await session.flush()
    connector = PluginDeployment(
        workspace_id=workspace.id,
        plugin_id=connector_plugin.id,
        name=f"Connector {suffix}",
        status=PluginDeploymentStatus.RUNNING,
    )
    session.add(connector)
    await session.flush()
    connector_config = {
        "websocket_url": "ws://maibot.test/ws",
        "api_key": "secret",
        "client_uuid": f"client-{suffix}",
        "tool_allowlist": connector_allowlist
        if connector_allowlist is not None
        else ["plugin.weather.query"],
    }
    connector_revision = PluginDeploymentRevision(
        deployment_id=connector.id,
        package_version_id=connector_package.id,
        revision_number=1,
        config_ciphertext=cipher.encrypt(json.dumps(connector_config)),
        config_fingerprint="b" * 64,
        scope={"workspace_id": str(workspace.id), "bot_account_ids": [str(account.id)]},
        grants=[MAIBOT_FORWARD_CAPABILITY],
        content_sha256="c" * 64,
    )
    session.add(connector_revision)
    await session.flush()
    connector.active_revision_id = connector_revision.id
    connector_activation = PluginRevisionActivation(
        deployment_id=connector.id,
        revision_id=connector_revision.id,
        activation_epoch=1,
        fencing_token=hashlib.sha256(f"fence:{suffix}".encode()).hexdigest(),
        status=PluginActivationStatus.ACTIVE,
        started_at=utc_now(),
    )
    session.add(connector_activation)
    await session.flush()

    target_plugin = Plugin(
        workspace_id=workspace.id,
        plugin_id="builtin.weather",
        name="Weather",
    )
    session.add(target_plugin)
    await session.flush()
    target_package = PluginPackageVersion(
        plugin_id=target_plugin.id,
        semantic_version="0.1.0",
        package_sha256=hashlib.sha256(f"weather:{suffix}".encode()).hexdigest(),
        manifest=_target_manifest(effect_class),
        package_path="unused",
        status=PluginPackageStatus.AVAILABLE,
    )
    session.add(target_package)
    await session.flush()
    target = PluginDeployment(
        workspace_id=workspace.id,
        plugin_id=target_plugin.id,
        name=f"Weather {suffix}",
        status=PluginDeploymentStatus.RUNNING,
    )
    session.add(target)
    await session.flush()
    target_revision = PluginDeploymentRevision(
        deployment_id=target.id,
        package_version_id=target_package.id,
        revision_number=1,
        config_ciphertext=cipher.encrypt("{}"),
        config_fingerprint="d" * 64,
        scope={"workspace_id": str(workspace.id), "bot_account_ids": [str(account.id)]},
        grants=target_grants if target_grants is not None else ["network.http.get"],
        content_sha256="e" * 64,
    )
    session.add(target_revision)
    await session.flush()
    target.active_revision_id = target_revision.id
    target_activation = PluginRevisionActivation(
        deployment_id=target.id,
        revision_id=target_revision.id,
        activation_epoch=1,
        fencing_token=hashlib.sha256(f"target-fence:{suffix}".encode()).hexdigest(),
        status=PluginActivationStatus.ACTIVE,
        started_at=utc_now(),
    )
    session.add(target_activation)
    await session.flush()

    authorization = OutboxAuthorizationContext(
        workspace_id=workspace.id,
        deployment_id=connector.id,
        deployment_revision_id=connector_revision.id,
        actor_principal_id=principal.id,
        chatroom_id=chatroom.id,
        resource_type=AclResourceType.PLUGIN,
        resource_id=MAIBOT_CONNECTOR_PLUGIN_ID,
    )
    source = MaiBotBridgeEnvelope(
        deployment_id=connector.id,
        deployment_revision_id=connector_revision.id,
        activation_id=connector_activation.id,
        bot_account_id=account.id,
        trace_id=inbox.trace_id,
        direction=MaiBotBridgeDirection.TO_MAIBOT,
        kind=MaiBotBridgeKind.MESSAGE,
        transport_message_id=f"transport:{suffix}",
        business_message_id=f"business:{suffix}",
        source_event_id=event.id,
        actor_principal_id=principal.id,
        chatroom_id=chatroom.id,
        target_wxid=chatroom.chatroom_id,
        authorization_context=authorization.model_dump(mode="json"),
        envelope={"payload": {"message_info": {"message_id": f"business:{suffix}"}}},
        payload_sha256="f" * 64,
        status=MaiBotBridgeStatus.SENT,
        expires_at=utc_now() + timedelta(minutes=5),
        available_at=utc_now(),
    )
    session.add(source)
    await session.flush()
    context_claims = MaiBotConversationContextClaims(source_envelope_id=source.id).model_dump_json()
    context_id = cipher.encrypt(context_claims).decode("ascii")

    policy = PolicyService()
    await policy.create_rule(
        session,
        AclRuleCreate(
            workspace_id=workspace.id,
            scope_type=AclScopeType.BOT_ACCOUNT,
            scope_id=str(account.id),
            resource_type=AclResourceType.PLUGIN,
            resource_id=MAIBOT_CONNECTOR_PLUGIN_ID,
            effect=AclEffect.ALLOW,
            reason="allow connector",
        ),
        created_by="test",
    )
    if allow_target:
        await policy.create_rule(
            session,
            AclRuleCreate(
                workspace_id=workspace.id,
                principal_id=principal.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.TOOL,
                resource_id="plugin.weather.query",
                effect=AclEffect.ALLOW,
                reason="allow weather",
            ),
            created_by="test",
        )
    return BridgeSeed(
        workspace_id=workspace.id,
        account_id=account.id,
        chatroom_id=chatroom.id,
        member_principal_id=principal.id,
        connector_deployment_id=connector.id,
        connector_revision_id=connector_revision.id,
        connector_activation_id=connector_activation.id,
        target_deployment_id=target.id,
        target_revision_id=target_revision.id,
        target_activation_id=target_activation.id,
        target_tool_name="plugin.weather.query",
        context_id=context_id,
        source_id=source.id,
    )


def _connector_manifest() -> dict[str, Any]:
    return {
        "id": MAIBOT_CONNECTOR_PLUGIN_ID,
        "name": "MaiBot Connector",
        "version": "0.1.0",
        "core_api": "1",
        "entrypoint": "plugin:create_plugin",
        "capabilities": [MAIBOT_FORWARD_CAPABILITY],
        "config_schema": {},
    }


def _target_manifest(effect_class: str) -> dict[str, Any]:
    return {
        "id": "builtin.weather",
        "name": "Weather",
        "version": "0.1.0",
        "core_api": "1",
        "entrypoint": "plugin:create_plugin",
        "capabilities": ["network.http.get"],
        "tools": [
            {
                "name": "plugin.weather.query",
                "schema_version": "1.0",
                "description": "query weather",
                "effect_class": effect_class,
                "input_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["city"],
                    "properties": {"city": {"type": "string", "minLength": 1}},
                },
                "output_schema": {"type": "object"},
                "required_capabilities": ["network.http.get"],
            }
        ],
        "config_schema": {},
    }
