from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy import func, select

from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ChatroomMembership,
    ConnectionStatus,
    GeweConnection,
    OutboxMessage,
    OutboxStatus,
    Workspace,
)
from wechat_bot.db.plugin_models import (
    Plugin,
    PluginDeployment,
    PluginDeploymentRevision,
    PluginDeploymentStatus,
    PluginPackageStatus,
    PluginPackageVersion,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    PolicyDecision,
    PrincipalType,
)
from wechat_bot.gewe.schemas import PostTextRequest, SentTextData
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.outbox.sender import SenderClientFactory, SenderOptions, SenderWorker
from wechat_bot.outbox.service import TEXT_REPLY_ACTION_TYPE, OutboxService
from wechat_bot.policy.schemas import AclRuleCreate, PrincipalCreate
from wechat_bot.policy.service import PolicyService


@dataclass(frozen=True, slots=True)
class AuthorizationSeed:
    message_id: UUID
    deployment_id: UUID
    revision_id: UUID
    package_id: UUID
    rule_id: UUID


class RecordingClient:
    def __init__(self, factory: RecordingClientFactory) -> None:
        self.factory = factory

    async def post_text(self, request: PostTextRequest) -> SentTextData:
        self.factory.requests.append(request)
        return SentTextData.model_validate(
            {
                "toWxid": request.to_wxid,
                "createTime": 1_703_841_160,
                "msgId": "9007199254740993",
                "newMsgId": "9007199254740995",
                "type": 1,
            }
        )


class RecordingClientContext:
    def __init__(self, factory: RecordingClientFactory) -> None:
        self.client = RecordingClient(factory)

    async def __aenter__(self) -> RecordingClient:
        return self.client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback


class RecordingClientFactory:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.requests: list[PostTextRequest] = []

    def __call__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float,
    ) -> RecordingClientContext:
        del base_url, timeout_seconds
        self.tokens.append(token)
        return RecordingClientContext(self)


async def _seed_authorized_outbox(
    app: FastAPI,
    settings: Settings,
) -> AuthorizationSeed:
    cipher = CredentialCipher.from_settings(settings)
    async with app.state.database.session_factory() as session, session.begin():
        workspace = Workspace(name="Authorized Sender", slug=f"authorized-{uuid4().hex}")
        session.add(workspace)
        await session.flush()
        token = "authorization-test-token"
        callback_secret = f"callback-{uuid4().hex}"
        connection = GeweConnection(
            workspace_id=workspace.id,
            name="Authorized Connection",
            api_base_url="https://api.gewe.test",
            token_ciphertext=cipher.encrypt(token),
            token_fingerprint=cipher.fingerprint(token),
            callback_secret_ciphertext=cipher.encrypt(callback_secret),
            callback_secret_hash=hashlib.sha256(callback_secret.encode("utf-8")).hexdigest(),
            status=ConnectionStatus.ACTIVE,
        )
        session.add(connection)
        await session.flush()
        account = BotAccount(
            gewe_connection_id=connection.id,
            app_id="wx_app_authorized",
            status=BotAccountStatus.ONLINE,
        )
        session.add(account)
        await session.flush()
        chatroom = Chatroom(
            bot_account_id=account.id,
            chatroom_id="authorized-room@chatroom",
            discovered_from="TEST",
            placeholder=False,
        )
        session.add(chatroom)
        await session.flush()
        session.add(
            ChatroomMembership(
                chatroom_id=chatroom.id,
                member_wxid="wxid_authorized_member",
                membership_epoch=1,
            )
        )
        policy = PolicyService()
        principal = await policy.create_principal(
            session,
            PrincipalCreate(
                workspace_id=workspace.id,
                principal_type=PrincipalType.GROUP_MEMBER,
                external_id="wxid_authorized_member",
            ),
        )

        plugin = Plugin(
            workspace_id=workspace.id,
            plugin_id="builtin.authorization-test",
            name="Authorization Test",
        )
        session.add(plugin)
        await session.flush()
        package = PluginPackageVersion(
            plugin_id=plugin.id,
            semantic_version="1.0.0",
            package_sha256=uuid4().hex + uuid4().hex,
            manifest={"id": "builtin.authorization-test"},
            package_path="unused-in-authorization-test",
            status=PluginPackageStatus.AVAILABLE,
        )
        session.add(package)
        await session.flush()
        deployment = PluginDeployment(
            workspace_id=workspace.id,
            plugin_id=plugin.id,
            name="Authorization Deployment",
            status=PluginDeploymentStatus.RUNNING,
        )
        session.add(deployment)
        await session.flush()
        revision = PluginDeploymentRevision(
            deployment_id=deployment.id,
            package_version_id=package.id,
            revision_number=1,
            config_ciphertext=b"encrypted",
            config_fingerprint="a" * 64,
            scope={"workspace_id": str(workspace.id)},
            grants=[TEXT_REPLY_ACTION_TYPE],
            content_sha256="b" * 64,
        )
        session.add(revision)
        await session.flush()
        deployment.active_revision_id = revision.id

        resource_id = "command.builtin.authorization-test.run"
        rule = await policy.create_rule(
            session,
            AclRuleCreate(
                workspace_id=workspace.id,
                principal_id=principal.id,
                scope_type=AclScopeType.CHATROOM,
                scope_id=str(chatroom.id),
                resource_type=AclResourceType.COMMAND,
                resource_id=resource_id,
                effect=AclEffect.ALLOW,
                reason="allow authorization sender test",
            ),
            created_by="test",
        )
        message = await OutboxService().enqueue_text(
            session,
            bot_account_id=account.id,
            trace_id=uuid4(),
            idempotency_key=f"authorization:{uuid4().hex}",
            target_wxid=chatroom.chatroom_id,
            text="authorized response",
            action_type=TEXT_REPLY_ACTION_TYPE,
            authorization_context=OutboxAuthorizationContext(
                workspace_id=workspace.id,
                deployment_id=deployment.id,
                deployment_revision_id=revision.id,
                actor_principal_id=principal.id,
                chatroom_id=chatroom.id,
                resource_type=AclResourceType.COMMAND,
                resource_id=resource_id,
                parent_plugin_id=plugin.plugin_id,
            ),
        )
        return AuthorizationSeed(
            message_id=message.id,
            deployment_id=deployment.id,
            revision_id=revision.id,
            package_id=package.id,
            rule_id=rule.id,
        )


def _worker(
    app: FastAPI,
    settings: Settings,
    factory: RecordingClientFactory,
) -> SenderWorker:
    return SenderWorker(
        session_factory=app.state.database.session_factory,
        cipher=CredentialCipher.from_settings(settings),
        options=SenderOptions(
            poll_interval_seconds=0.01,
            per_minute_limit=100,
            target_interval_seconds=0,
            group_interval_min_seconds=0,
            group_interval_max_seconds=0,
            retry_jitter_ratio=0,
            lease_seconds=61,
            request_timeout_seconds=1,
        ),
        client_factory=cast(SenderClientFactory, factory),
    )


async def _stored_message(app: FastAPI, message_id: UUID) -> OutboxMessage:
    async with app.state.database.session_factory() as session:
        message = await session.get(OutboxMessage, message_id)
        assert message is not None
        session.expunge(message)
        return message


async def test_sender_rechecks_policy_and_sends_when_context_is_still_valid(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.SENT
    assert message.last_attempt_started_at is not None
    assert message.last_attempt_finished_at is not None
    assert message.provider_message_id == "9007199254740993"
    assert message.provider_new_message_id == "9007199254740995"
    assert message.provider_create_time == 1_703_841_160
    assert message.provider_message_type == 1
    assert len(factory.requests) == 1
    assert factory.tokens == ["authorization-test-token"]
    async with app.state.database.session_factory() as session:
        decision_count = await session.scalar(select(func.count()).select_from(PolicyDecision))
    assert decision_count == 1


async def test_stopped_deployment_cancels_without_calling_gewe(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    async with app.state.database.session_factory() as session, session.begin():
        deployment = await session.get(PluginDeployment, seed.deployment_id)
        assert deployment is not None
        deployment.status = PluginDeploymentStatus.STOPPED
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.CANCELLED
    assert message.last_error_code == "POLICY_CHANGED"
    assert factory.requests == []
    assert factory.tokens == []


async def test_changed_active_revision_cancels_without_calling_gewe(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    async with app.state.database.session_factory() as session, session.begin():
        deployment = await session.get(PluginDeployment, seed.deployment_id)
        assert deployment is not None
        revision = PluginDeploymentRevision(
            deployment_id=deployment.id,
            package_version_id=seed.package_id,
            revision_number=2,
            config_ciphertext=b"encrypted-v2",
            config_fingerprint="c" * 64,
            scope={},
            grants=[TEXT_REPLY_ACTION_TYPE],
            content_sha256="d" * 64,
        )
        session.add(revision)
        await session.flush()
        deployment.active_revision_id = revision.id
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.CANCELLED
    assert message.last_error_code == "POLICY_CHANGED"
    assert factory.requests == []


async def test_revoked_acl_cancels_without_calling_gewe(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    async with app.state.database.session_factory() as session, session.begin():
        await PolicyService().revoke_rule(
            session,
            seed.rule_id,
            revoked_by="test",
        )
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.CANCELLED
    assert message.last_error_code == "POLICY_CHANGED"
    assert factory.requests == []
    async with app.state.database.session_factory() as session:
        decision = await session.scalar(
            select(PolicyDecision).order_by(PolicyDecision.created_at.desc())
        )
    assert decision is not None
    assert decision.effect is AclEffect.DENY


async def test_malformed_authorization_context_is_cancelled_conservatively(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    async with app.state.database.session_factory() as session, session.begin():
        message = await session.get(OutboxMessage, seed.message_id)
        assert message is not None
        message.authorization_context = {"schema_version": "1.0"}
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.CANCELLED
    assert message.last_error_code == "POLICY_CHANGED"
    assert factory.requests == []


async def test_authorization_context_cannot_be_reused_for_another_target(
    app: FastAPI,
    client: object,
    settings: Settings,
) -> None:
    del client
    seed = await _seed_authorized_outbox(app, settings)
    async with app.state.database.session_factory() as session, session.begin():
        message = await session.get(OutboxMessage, seed.message_id)
        assert message is not None
        message.target_wxid = "different-room@chatroom"
    factory = RecordingClientFactory()

    assert await _worker(app, settings, factory).run_once() == 1

    message = await _stored_message(app, seed.message_id)
    assert message.status is OutboxStatus.CANCELLED
    assert message.last_error_code == "POLICY_CHANGED"
    assert message.attempt_count == 0
    assert factory.requests == []
