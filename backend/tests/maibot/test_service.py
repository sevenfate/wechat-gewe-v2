from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import utc_now
from wechat_bot.db.maibot_models import (
    MaiBotBridgeDirection,
    MaiBotBridgeEnvelope,
    MaiBotBridgeKind,
    MaiBotBridgeStatus,
    MaiBotConnectionState,
    MaiBotConnectionStatus,
)
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ConversationType,
    GeweConnection,
    InboxStatus,
    NormalizedEvent,
    OutboxMessage,
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
    PrincipalType,
)
from wechat_bot.events.dispatcher import EventDispatcher
from wechat_bot.events.outbox_sink import OutboxTextActionSink
from wechat_bot.maibot.constants import (
    MAIBOT_API_KEY_PLACEHOLDER,
    MAIBOT_CONNECTOR_PLUGIN_ID,
    MAIBOT_FORWARD_CAPABILITY,
    MAIBOT_PROACTIVE_CAPABILITY,
)
from wechat_bot.maibot.mapping import build_ack_envelope
from wechat_bot.maibot.runtime import MaiBotConnectionWorker
from wechat_bot.maibot.schemas import MaiBotActivationContext, MaiBotConnectorConfig
from wechat_bot.maibot.service import (
    MaiBotBridgeService,
    MaiBotStaleActivationError,
)
from wechat_bot.outbox.service import TEXT_ACTION_TYPE, TEXT_REPLY_ACTION_TYPE
from wechat_bot.policy.schemas import AclRuleCreate, PrincipalCreate
from wechat_bot.policy.service import PolicyService


class NeverCalledInvoker:
    async def call(
        self,
        deployment_id: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        del deployment_id, method, params
        raise AssertionError("managed MaiBot connector must not use synchronous plugin RPC")


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        raise AssertionError("this test drives received frames directly")

    async def close(self) -> None:
        return


class RecordingBridgeService(MaiBotBridgeService):
    def __init__(self, cipher: CredentialCipher) -> None:
        super().__init__(cipher)
        self.connection_statuses: list[MaiBotConnectionStatus] = []

    async def set_connection_status(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        status: MaiBotConnectionStatus,
        error_code: str | None = None,
    ) -> MaiBotConnectionState | None:
        self.connection_statuses.append(status)
        return await super().set_connection_status(
            session,
            context=context,
            status=status,
            error_code=error_code,
        )


@dataclass(frozen=True, slots=True)
class BridgeSeed:
    workspace_id: UUID
    account_id: UUID
    event_id: UUID
    inbox_id: UUID
    deployment_id: UUID
    revision_id: UUID
    activation_id: UUID
    allow_rule_id: UUID | None
    group_external_id: str


async def test_dispatcher_queues_only_after_connector_acl(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        allowed = await _seed(session, cipher=cipher, allow_connector=True)
        denied = await _seed(
            session,
            cipher=cipher,
            allow_connector=False,
            workspace_id=allowed.workspace_id,
        )
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )

        allowed_result = await dispatcher.dispatch(session, allowed.event_id)
        denied_result = await dispatcher.dispatch(session, denied.event_id)
        await session.commit()

        rows = list(
            await session.scalars(
                select(MaiBotBridgeEnvelope).order_by(MaiBotBridgeEnvelope.created_at)
            )
        )

    assert allowed_result.invoked_plugins == 1
    assert allowed_result.accepted_actions == 1
    assert denied_result.denied_plugins == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.deployment_id == allowed.deployment_id
    assert row.direction is MaiBotBridgeDirection.TO_MAIBOT
    assert row.kind is MaiBotBridgeKind.MESSAGE
    assert row.status is MaiBotBridgeStatus.PENDING
    assert row.transport_message_id != row.business_message_id
    assert row.business_message_id == "gewe:app-maibot:9007199254740993"
    assert row.envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
    assert row.envelope["payload"]["message_dim"]["api_key"] == MAIBOT_API_KEY_PLACEHOLDER


async def test_queued_message_is_cancelled_when_acl_is_revoked_before_delivery(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        assert seed.allow_rule_id is not None
        await PolicyService().revoke_rule(
            session,
            seed.allow_rule_id,
            revoked_by="test",
        )
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        claimed = await service.claim_next(session, context=context)
        row = await session.scalar(select(MaiBotBridgeEnvelope))

    assert claimed is None
    assert row is not None
    assert row.status is MaiBotBridgeStatus.CANCELLED
    assert row.last_error_code == "MAIBOT_POLICY_CHANGED"


async def test_unsent_message_cannot_be_suppressed_by_early_ack(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        source = await session.scalar(select(MaiBotBridgeEnvelope))
        assert source is not None
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        await service.acknowledge(
            session,
            context=context,
            transport_message_id=source.transport_message_id,
        )

    assert source.status is MaiBotBridgeStatus.PENDING


async def test_outbound_message_with_wrong_api_key_is_rejected_and_sanitized(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        outbound = _outbound(
            envelope_id="wrong-frame-key",
            target_wxid=seed.group_external_id,
            reply_to=None,
            text="must not leave platform",
        )
        outbound["payload"]["message_dim"]["api_key"] = "wrong-api-key"

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=outbound,
        )

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_INVALID_ENVELOPE"
    assert received.envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
    assert received.envelope["payload"]["message_dim"]["api_key"] == MAIBOT_API_KEY_PLACEHOLDER


async def test_reply_uses_original_context_and_enters_outbox(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        source = await session.scalar(select(MaiBotBridgeEnvelope))
        assert source is not None
        source.status = MaiBotBridgeStatus.ACKED
        source.acked_at = utc_now()
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-reply-1",
                target_wxid=seed.group_external_id,
                reply_to=source.business_message_id,
                text="收到，我来回答。",  # noqa: RUF001
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox = await session.scalar(select(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.ACCEPTED
    assert received.kind is MaiBotBridgeKind.REPLY
    assert received.source_envelope_id == source.id
    assert received.last_error_code == "MAIBOT_QUOTE_DOWNGRADED_TO_TEXT"
    assert outbox is not None
    assert outbox.action_type == TEXT_REPLY_ACTION_TYPE
    assert outbox.target_wxid == seed.group_external_id
    assert outbox.payload == {"text": "收到，我来回答。", "at_wxids": []}  # noqa: RUF001
    assert outbox.authorization_context == source.authorization_context
    assert received.trace_id == source.trace_id
    assert outbox.trace_id == source.trace_id


async def test_private_reply_uses_platform_signed_conversation_context(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        event = await session.get(NormalizedEvent, seed.event_id)
        assert event is not None
        private_wxid = "wxid_private_friend"
        event.conversation_type = ConversationType.PRIVATE
        event.conversation_id = private_wxid
        event.actor_wxid = private_wxid
        source = await _dispatch_source(session, service=service, seed=seed)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-private-reply",
                target_wxid=private_wxid,
                target_kind="PRIVATE",
                reply_to=source.business_message_id,
                text="private response",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox = await session.scalar(select(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.ACCEPTED
    assert received.contact_id is not None
    assert received.chatroom_id is None
    assert outbox is not None
    assert outbox.target_wxid == private_wxid
    assert outbox.trace_id == source.trace_id


async def test_reply_cannot_switch_to_another_receiver(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        source = await session.scalar(select(MaiBotBridgeEnvelope))
        assert source is not None
        source.status = MaiBotBridgeStatus.SENT
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-cross-target",
                target_wxid="attacker-selected@chatroom",
                reply_to=source.business_message_id,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_REPLY_TARGET_MISMATCH"
    assert outbox_count == 0


async def test_forged_conversation_context_is_rejected(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        source = await _dispatch_source(session, service=service, seed=seed)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        context_id = _connector_context_from(source)
        forged_context_id = ("A" if context_id[0] != "A" else "B") + context_id[1:]

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-forged-context",
                target_wxid=seed.group_external_id,
                reply_to=source.business_message_id,
                text="must not leave platform",
                connector_context_id=forged_context_id,
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert context_id != str(source.id)
    assert str(source.id) not in context_id
    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_CONTEXT_INVALID"
    assert outbox_count == 0


async def test_conversation_context_cannot_cross_connection_or_deployment(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        source_seed = await _seed(session, cipher=cipher, allow_connector=True)
        other_seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            workspace_id=source_seed.workspace_id,
        )
        source = await _dispatch_source(session, service=service, seed=source_seed)
        other_context = await service.activation_context(
            session,
            deployment_id=other_seed.deployment_id,
            activation_epoch=1,
        )
        assert other_context is not None

        received = await service.receive_standard(
            session,
            context=other_context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-cross-connection-context",
                target_wxid=source_seed.group_external_id,
                reply_to=source.business_message_id,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_CONTEXT_SCOPE_MISMATCH"
    assert outbox_count == 0


async def test_expired_conversation_context_is_rejected(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        source = await _dispatch_source(session, service=service, seed=seed)
        source.expires_at = utc_now() - timedelta(seconds=1)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-expired-context",
                target_wxid=seed.group_external_id,
                reply_to=source.business_message_id,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_SOURCE_CONTEXT_EXPIRED"
    assert outbox_count == 0


async def test_conversation_context_is_single_use_but_transport_replay_is_idempotent(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        source = await _dispatch_source(session, service=service, seed=seed)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        first_envelope = _outbound(
            envelope_id="maibot-context-first-use",
            target_wxid=seed.group_external_id,
            reply_to=source.business_message_id,
            text="first response",
            connector_context_id=_connector_context_from(source),
        )

        first = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=first_envelope,
        )
        replay = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=first_envelope,
        )
        second = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-context-second-use",
                target_wxid=seed.group_external_id,
                reply_to=source.business_message_id,
                text="second response",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert first.status is MaiBotBridgeStatus.ACCEPTED
    assert replay.id == first.id
    assert second.status is MaiBotBridgeStatus.REJECTED
    assert second.last_error_code == "MAIBOT_CONTEXT_ALREADY_USED"
    assert outbox_count == 1


async def test_reply_is_rejected_when_source_acl_is_revoked_after_context_issue(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        source = await _dispatch_source(session, service=service, seed=seed)
        assert seed.allow_rule_id is not None
        await PolicyService().revoke_rule(
            session,
            seed.allow_rule_id,
            revoked_by="test",
        )
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(),
            envelope=_outbound(
                envelope_id="maibot-revoked-context",
                target_wxid=seed.group_external_id,
                reply_to=source.business_message_id,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_REPLY_POLICY_DENIED"
    assert outbox_count == 0


async def test_proactive_target_without_platform_context_is_rejected(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            enable_proactive=True,
        )
        await _dispatch_source(session, service=service, seed=seed)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        outbound = _outbound(
            envelope_id="maibot-proactive-without-context",
            target_wxid=seed.group_external_id,
            reply_to=None,
            text="must not leave platform",
        )
        del outbound["payload"]["message_info"]["additional_config"]

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=outbound,
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_CONTEXT_REQUIRED"
    assert outbox_count == 0


async def test_proactive_message_uses_connector_principal_and_capability_acl(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            enable_proactive=True,
        )
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        source = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT
            )
        )
        assert source is not None
        source.status = MaiBotBridgeStatus.SENT
        chatroom = await session.scalar(
            select(Chatroom).where(Chatroom.chatroom_id == seed.group_external_id)
        )
        assert chatroom is not None
        connector = await PolicyService().create_principal(
            session,
            PrincipalCreate(
                workspace_id=seed.workspace_id,
                principal_type=PrincipalType.CONNECTOR,
                external_id=f"maibot:{seed.deployment_id}",
                display_name="MaiBot Connector",
            ),
        )
        await PolicyService().create_rule(
            session,
            AclRuleCreate(
                workspace_id=seed.workspace_id,
                principal_id=connector.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.CAPABILITY,
                resource_id=MAIBOT_PROACTIVE_CAPABILITY,
                effect=AclEffect.ALLOW,
                reason="allow proactive message in this test group",
            ),
            created_by="test",
        )
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=_outbound(
                envelope_id="maibot-proactive-1",
                target_wxid=seed.group_external_id,
                reply_to=None,
                text="大家晚上好",
                connector_context_id=_connector_context_from(source),
                include_bot_user=True,
            ),
        )
        outbox = await session.scalar(
            select(OutboxMessage).where(OutboxMessage.action_type == TEXT_ACTION_TYPE)
        )

    assert received.status is MaiBotBridgeStatus.ACCEPTED
    assert received.kind is MaiBotBridgeKind.PROACTIVE
    assert outbox is not None
    assert outbox.authorization_context is not None
    assert outbox.authorization_context["actor_principal_id"] == str(connector.id)
    assert outbox.authorization_context["resource_type"] == "CAPABILITY"
    assert outbox.authorization_context["resource_id"] == MAIBOT_PROACTIVE_CAPABILITY


async def test_proactive_message_is_rejected_after_connector_acl_revocation(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            enable_proactive=True,
        )
        source = await _dispatch_source(session, service=service, seed=seed)
        chatroom = await session.scalar(
            select(Chatroom).where(Chatroom.chatroom_id == seed.group_external_id)
        )
        assert chatroom is not None
        connector = await PolicyService().create_principal(
            session,
            PrincipalCreate(
                workspace_id=seed.workspace_id,
                principal_type=PrincipalType.CONNECTOR,
                external_id=f"maibot:{seed.deployment_id}",
                display_name="MaiBot Connector",
            ),
        )
        rule = await PolicyService().create_rule(
            session,
            AclRuleCreate(
                workspace_id=seed.workspace_id,
                principal_id=connector.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.CAPABILITY,
                resource_id=MAIBOT_PROACTIVE_CAPABILITY,
                effect=AclEffect.ALLOW,
                reason="temporarily allow proactive message",
            ),
            created_by="test",
        )
        await PolicyService().revoke_rule(session, rule.id, revoked_by="test")
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=_outbound(
                envelope_id="maibot-proactive-after-revoke",
                target_wxid=seed.group_external_id,
                reply_to=None,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_PROACTIVE_POLICY_DENIED"
    assert outbox_count == 0


async def test_proactive_message_accepts_platform_route_without_connector_context(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            enable_proactive=True,
        )
        await _dispatch_source(session, service=service, seed=seed)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        chatroom = await session.scalar(
            select(Chatroom).where(Chatroom.chatroom_id == seed.group_external_id)
        )
        assert chatroom is not None
        connector = await PolicyService().create_principal(
            session,
            PrincipalCreate(
                workspace_id=seed.workspace_id,
                principal_type=PrincipalType.CONNECTOR,
                external_id=f"maibot:{seed.deployment_id}",
                display_name="MaiBot Connector",
            ),
        )
        await PolicyService().create_rule(
            session,
            AclRuleCreate(
                workspace_id=seed.workspace_id,
                principal_id=connector.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.CAPABILITY,
                resource_id=MAIBOT_PROACTIVE_CAPABILITY,
                effect=AclEffect.ALLOW,
                reason="allow routed proactive message in this test group",
            ),
            created_by="test",
        )
        outbound = _outbound(
            envelope_id="maibot-proactive-platform-route",
            target_wxid=seed.group_external_id,
            reply_to=None,
            text="平台路由主动消息",
            include_bot_user=True,
        )
        additional_config = outbound["payload"]["message_info"]["additional_config"]
        assert isinstance(additional_config, dict)
        del additional_config["wechat_bot_connector_context_id"]
        additional_config.update(
            {
                "platform_io_account_id": "app-maibot",
                "platform_io_scope": str(seed.deployment_id),
                "platform_io_target_group_id": seed.group_external_id,
            }
        )

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=outbound,
        )
        outbox = await session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.idempotency_key.endswith(":proactive:maibot-proactive-platform-route")
            )
        )

    assert received.status is MaiBotBridgeStatus.ACCEPTED
    assert received.source_envelope_id is None
    assert outbox is not None
    assert outbox.target_wxid == seed.group_external_id
    assert outbox.authorization_context["resource_id"] == MAIBOT_PROACTIVE_CAPABILITY


async def test_proactive_message_requires_dedicated_revision_grant(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(
            session,
            cipher=cipher,
            allow_connector=True,
            enable_proactive=True,
        )
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        revision = await session.get(PluginDeploymentRevision, seed.revision_id)
        assert revision is not None
        revision.grants = [
            grant for grant in revision.grants if grant != MAIBOT_PROACTIVE_CAPABILITY
        ]
        await session.flush()
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        source = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT
            )
        )
        assert source is not None
        source.status = MaiBotBridgeStatus.SENT

        received = await service.receive_standard(
            session,
            context=context,
            config=_config(enable_proactive=True),
            envelope=_outbound(
                envelope_id="maibot-proactive-without-grant",
                target_wxid=seed.group_external_id,
                reply_to=None,
                text="must not leave platform",
                connector_context_id=_connector_context_from(source),
            ),
        )
        outbox_count = await session.scalar(select(func.count()).select_from(OutboxMessage))

    assert received.status is MaiBotBridgeStatus.REJECTED
    assert received.last_error_code == "MAIBOT_PROACTIVE_GRANT_MISSING"
    assert outbox_count == 0


async def test_stale_activation_cannot_claim_or_submit_messages(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        assert context is not None
        deployment = await session.get(PluginDeployment, seed.deployment_id)
        assert deployment is not None
        deployment.status = PluginDeploymentStatus.STOPPED
        await session.flush()

        claimed = await service.claim_next(session, context=context)
        with pytest.raises(MaiBotStaleActivationError):
            await service.receive_standard(
                session,
                context=context,
                config=_config(),
                envelope=_outbound(
                    envelope_id="stale-reply",
                    target_wxid=seed.group_external_id,
                    reply_to="gewe:app-maibot:9007199254740993",
                    text="must not be accepted",
                ),
            )
        rows = list(await session.scalars(select(MaiBotBridgeEnvelope)))

    assert claimed is None
    assert len(rows) == 1
    assert rows[0].direction is MaiBotBridgeDirection.TO_MAIBOT
    assert rows[0].status is MaiBotBridgeStatus.PENDING


async def test_connector_safely_rejects_event_without_sender_identity(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        event = await session.get(NormalizedEvent, seed.event_id)
        assert event is not None
        event.actor_wxid = None
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )

        result = await dispatcher.dispatch(session, seed.event_id)
        count = await session.scalar(select(func.count()).select_from(MaiBotBridgeEnvelope))

    assert result.invoked_plugins == 1
    assert result.rejected_actions == 1
    assert count == 0


async def test_runtime_materializes_secret_acknowledges_and_enqueues_reply(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = MaiBotBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        dispatcher = EventDispatcher(
            invoker=NeverCalledInvoker(),
            action_sink=OutboxTextActionSink(),
            maibot_sink=service,
        )
        await dispatcher.dispatch(session, seed.event_id)
        await session.commit()
        context = await service.activation_context(
            session,
            deployment_id=seed.deployment_id,
            activation_epoch=1,
        )
        source = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT
            )
        )
        assert context is not None
        assert source is not None
        source_id = source.id
        transport_id = source.transport_message_id
        business_message_id = source.business_message_id
        assert business_message_id is not None
        connector_context_id = _connector_context_from(source)

    worker = MaiBotConnectionWorker(
        deployment_id=seed.deployment_id,
        activation_epoch=1,
        config=_config(),
        session_factory=database.session_factory,
        service=service,
    )
    socket = RecordingSocket()
    await worker._send_once(socket, context)

    assert len(socket.sent) == 1
    wire = json.loads(socket.sent[0])
    assert wire["meta"]["sender_user"] == "test-maibot-api-key"
    assert wire["payload"]["message_dim"]["api_key"] == "test-maibot-api-key"
    async with database.session_factory() as session:
        persisted = await session.get(MaiBotBridgeEnvelope, source_id)
        assert persisted is not None
        assert persisted.status is MaiBotBridgeStatus.SENT
        assert persisted.envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
        assert persisted.envelope["payload"]["message_dim"]["api_key"] == MAIBOT_API_KEY_PLACEHOLDER

    ack = build_ack_envelope(
        envelope_id="maibot-ack-1",
        acked_envelope_id=transport_id,
        connection_uuid="maibot-server",
        timestamp=1_788_055_201.0,
    )
    await worker._receive_once(socket, context, json.dumps(ack))
    await worker._receive_once(
        socket,
        context,
        json.dumps(
            _outbound(
                envelope_id="maibot-runtime-reply",
                target_wxid=seed.group_external_id,
                reply_to=business_message_id,
                text="runtime reply",
                connector_context_id=connector_context_id,
            ),
            ensure_ascii=False,
        ),
    )

    async with database.session_factory() as session:
        persisted = await session.get(MaiBotBridgeEnvelope, source_id)
        received = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.FROM_MAIBOT
            )
        )
        outbox = await session.scalar(select(OutboxMessage))

    assert persisted is not None
    assert persisted.status is MaiBotBridgeStatus.ACKED
    assert received is not None
    assert received.status is MaiBotBridgeStatus.ACCEPTED
    assert received.envelope["meta"]["sender_user"] == MAIBOT_API_KEY_PLACEHOLDER
    assert outbox is not None
    assert outbox.payload == {"text": "runtime reply", "at_wxids": []}
    assert len(socket.sent) == 2
    assert json.loads(socket.sent[1])["type"] == "sys_ack"


async def test_runtime_disconnect_enters_backoff_without_escaping_worker(
    app: FastAPI,
    client: object,
) -> None:
    del client
    database = app.state.database
    cipher = CredentialCipher.from_settings(app.state.settings)
    service = RecordingBridgeService(cipher)
    async with database.session_factory() as session:
        seed = await _seed(session, cipher=cipher, allow_connector=True)
        await session.commit()

    class FailingSocketContext:
        async def __aenter__(self) -> RecordingSocket:
            raise ConnectionError("MaiBot is unavailable")

        async def __aexit__(self, *args: object) -> None:
            return

    def failing_socket_factory(config: MaiBotConnectorConfig) -> FailingSocketContext:
        del config
        return FailingSocketContext()

    config = _config().model_copy(
        update={"reconnect_initial_seconds": 0.05, "reconnect_max_seconds": 0.05}
    )
    worker = MaiBotConnectionWorker(
        deployment_id=seed.deployment_id,
        activation_epoch=1,
        config=config,
        session_factory=database.session_factory,
        service=service,
        socket_factory=failing_socket_factory,
    )
    await worker.start()
    try:
        for _ in range(100):
            if MaiBotConnectionStatus.BACKOFF in service.connection_statuses:
                break
            await asyncio.sleep(0.01)
        assert MaiBotConnectionStatus.BACKOFF in service.connection_statuses
        assert worker.running
    finally:
        await worker.stop()

    async with database.session_factory() as session:
        state = await session.scalar(
            select(MaiBotConnectionState).where(
                MaiBotConnectionState.deployment_id == seed.deployment_id
            )
        )
    assert state is not None
    assert state.status is MaiBotConnectionStatus.STOPPED


async def _dispatch_source(
    session: AsyncSession,
    *,
    service: MaiBotBridgeService,
    seed: BridgeSeed,
) -> MaiBotBridgeEnvelope:
    dispatcher = EventDispatcher(
        invoker=NeverCalledInvoker(),
        action_sink=OutboxTextActionSink(),
        maibot_sink=service,
    )
    await dispatcher.dispatch(session, seed.event_id)
    source = await session.scalar(
        select(MaiBotBridgeEnvelope).where(
            MaiBotBridgeEnvelope.deployment_id == seed.deployment_id,
            MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT,
        )
    )
    assert source is not None
    source.status = MaiBotBridgeStatus.SENT
    await session.flush()
    return source


async def _seed(
    session: AsyncSession,
    *,
    cipher: CredentialCipher,
    allow_connector: bool,
    enable_proactive: bool = False,
    workspace_id: UUID | None = None,
) -> BridgeSeed:
    suffix = uuid7().hex
    if workspace_id is None:
        workspace = Workspace(name="MaiBot", slug=f"maibot-{suffix}")
        session.add(workspace)
        await session.flush()
    else:
        workspace = await session.get(Workspace, workspace_id)
        assert workspace is not None
    connection = GeweConnection(
        workspace_id=workspace.id,
        name=f"Primary {suffix}",
        api_base_url="https://api.gewe.test",
        token_ciphertext=b"encrypted-gewe-token",
        token_fingerprint="0123456789abcdef",
        callback_secret_ciphertext=b"encrypted-callback-secret",
        callback_secret_hash=uuid7().hex + uuid7().hex,
    )
    session.add(connection)
    await session.flush()
    account = BotAccount(
        gewe_connection_id=connection.id,
        app_id="app-maibot",
        wxid="wxid_bot",
        status=BotAccountStatus.ONLINE,
    )
    session.add(account)
    await session.flush()
    inbox = WebhookInbox(
        gewe_connection_id=connection.id,
        app_id=account.app_id,
        new_msg_id="9007199254740993",
        dedup_key=f"message:{suffix}",
        payload_sha256="a" * 64,
        schema_version="v2",
        raw_payload={"redacted": True},
        trace_id=uuid7(),
        status=InboxStatus.NORMALIZED,
    )
    session.add(inbox)
    await session.flush()
    group_external_id = f"{suffix}@chatroom"
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
        occurred_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        content={"msg_type": "1", "raw_content": "wxid_member:\n大家好"},
        raw_ref=f"db:webhook_inbox/{inbox.id}",
    )
    session.add(event)

    plugin = await session.scalar(
        select(Plugin).where(
            Plugin.workspace_id == workspace.id,
            Plugin.plugin_id == MAIBOT_CONNECTOR_PLUGIN_ID,
        )
    )
    if plugin is None:
        plugin = Plugin(
            workspace_id=workspace.id,
            plugin_id=MAIBOT_CONNECTOR_PLUGIN_ID,
            name="MaiBot Connector",
        )
        session.add(plugin)
        await session.flush()
    package = await session.scalar(
        select(PluginPackageVersion).where(PluginPackageVersion.plugin_id == plugin.id)
    )
    if package is None:
        package = PluginPackageVersion(
            plugin_id=plugin.id,
            semantic_version="0.1.0",
            package_sha256=uuid7().hex + uuid7().hex,
            manifest=_manifest(),
            package_path="unused-in-maibot-service-test",
            status=PluginPackageStatus.AVAILABLE,
        )
        session.add(package)
        await session.flush()
    deployment = PluginDeployment(
        workspace_id=workspace.id,
        plugin_id=plugin.id,
        name=f"MaiBot {suffix}",
        status=PluginDeploymentStatus.RUNNING,
    )
    session.add(deployment)
    await session.flush()
    raw_config = _config(enable_proactive=enable_proactive).model_dump(mode="json")
    raw_config["api_key"] = "test-maibot-api-key"
    revision = PluginDeploymentRevision(
        deployment_id=deployment.id,
        package_version_id=package.id,
        revision_number=1,
        config_ciphertext=cipher.encrypt(
            json.dumps(raw_config, ensure_ascii=False, sort_keys=True)
        ),
        config_fingerprint="b" * 64,
        scope={
            "workspace_id": str(workspace.id),
            "bot_account_ids": [str(account.id)],
        },
        grants=[
            MAIBOT_FORWARD_CAPABILITY,
            TEXT_REPLY_ACTION_TYPE,
            TEXT_ACTION_TYPE,
            *([MAIBOT_PROACTIVE_CAPABILITY] if enable_proactive else []),
        ],
        content_sha256="c" * 64,
    )
    session.add(revision)
    await session.flush()
    deployment.active_revision_id = revision.id
    activation = PluginRevisionActivation(
        deployment_id=deployment.id,
        revision_id=revision.id,
        activation_epoch=1,
        fencing_token=uuid7().hex + uuid7().hex,
        status=PluginActivationStatus.ACTIVE,
        started_at=utc_now(),
    )
    session.add(activation)
    await session.flush()

    allow_rule_id: UUID | None = None
    if allow_connector:
        rule = await PolicyService().create_rule(
            session,
            AclRuleCreate(
                workspace_id=workspace.id,
                scope_type=AclScopeType.BOT_ACCOUNT,
                scope_id=str(account.id),
                resource_type=AclResourceType.PLUGIN,
                resource_id=MAIBOT_CONNECTOR_PLUGIN_ID,
                effect=AclEffect.ALLOW,
                reason="allow connector in test",
            ),
            created_by="test",
        )
        allow_rule_id = rule.id
    await session.flush()
    return BridgeSeed(
        workspace_id=workspace.id,
        account_id=account.id,
        event_id=event.id,
        inbox_id=inbox.id,
        deployment_id=deployment.id,
        revision_id=revision.id,
        activation_id=activation.id,
        allow_rule_id=allow_rule_id,
        group_external_id=group_external_id,
    )


def _config(*, enable_proactive: bool = False) -> MaiBotConnectorConfig:
    return MaiBotConnectorConfig(
        websocket_url="ws://maibot.test:8090/ws",
        api_key="test-maibot-api-key",
        client_uuid="wechat-bot-test-connector",
        enable_proactive_messages=enable_proactive,
    )


def _manifest() -> dict[str, Any]:
    return {
        "id": MAIBOT_CONNECTOR_PLUGIN_ID,
        "name": "MaiBot Connector",
        "version": "0.1.0",
        "core_api": "1",
        "entrypoint": "plugin:create_plugin",
        "events": ["gewe.message.text"],
        "commands": [],
        "tools": [],
        "capabilities": [
            MAIBOT_FORWARD_CAPABILITY,
            TEXT_REPLY_ACTION_TYPE,
            TEXT_ACTION_TYPE,
            MAIBOT_PROACTIVE_CAPABILITY,
        ],
        "timeout_seconds": 5,
        "config_schema": {},
    }


def _outbound(
    *,
    envelope_id: str,
    target_wxid: str,
    reply_to: str | None,
    text: str,
    connector_context_id: str = "invalid-test-context",
    target_kind: str = "GROUP",
    include_bot_user: bool = False,
) -> dict[str, Any]:
    segments: list[dict[str, str]] = []
    if reply_to is not None:
        segments.append({"type": "reply", "data": reply_to})
    segments.append({"type": "text", "data": text})
    receiver_info = (
        {"group_info": {"platform": "gewe", "group_id": target_wxid}}
        if target_kind == "GROUP"
        else {"user_info": {"platform": "gewe", "user_id": target_wxid}}
    )
    if include_bot_user:
        receiver_info["user_info"] = {"platform": "gewe", "user_id": "app-maibot"}
    return {
        "ver": 1,
        "msg_id": envelope_id,
        "type": "sys_std",
        "meta": {
            "sender_user": "test-maibot-api-key",
            "platform": "gewe",
            "timestamp": 1_788_055_200.25,
        },
        "payload": {
            "message_info": {
                "platform": "gewe",
                "message_id": f"business:{envelope_id}",
                "time": 1_788_055_200.25,
                "additional_config": {
                    "wechat_bot_connector_context_id": connector_context_id,
                },
                "receiver_info": receiver_info,
            },
            "message_segment": {"type": "seglist", "data": segments},
            "message_dim": {
                "api_key": "test-maibot-api-key",
                "platform": "gewe",
            },
        },
    }


def _connector_context_from(source: MaiBotBridgeEnvelope) -> str:
    return str(
        source.envelope["payload"]["message_info"]["additional_config"][
            "wechat_bot_connector_context_id"
        ]
    )
