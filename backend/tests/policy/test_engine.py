from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.auth.service import AuthPrincipal
from wechat_bot.core.config import Settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.db.base import Base
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ChatroomMembership,
    GeweConnection,
    Workspace,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclResourceType,
    AclScopeType,
    Principal,
    PrincipalType,
)
from wechat_bot.db.session import Database
from wechat_bot.directory.service import DirectoryService
from wechat_bot.gewe.schemas import ChatroomMember
from wechat_bot.policy.schemas import AclEvaluationRequest, AclRuleCreate, PrincipalCreate
from wechat_bot.policy.service import PolicyService


@dataclass(frozen=True, slots=True)
class PolicyFixture:
    workspace_id: UUID
    bot_account_id: UUID
    chatroom_id: UUID
    member_one: Principal
    member_two: Principal


async def test_group_allow_and_member_deny_follow_specificity(settings: Settings) -> None:
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with database.session_factory() as session:
            fixture = await _seed(session)
            service = PolicyService()
            await service.create_rule(
                session,
                _rule(
                    fixture,
                    resource_type=AclResourceType.PLUGIN,
                    resource_id="plugin.weather",
                    effect=AclEffect.ALLOW,
                    reason="群允许天气",
                ),
                created_by="test",
            )
            await service.create_rule(
                session,
                _rule(
                    fixture,
                    principal_id=fixture.member_one.id,
                    resource_type=AclResourceType.PLUGIN,
                    resource_id="plugin.weather",
                    effect=AclEffect.DENY,
                    reason="成员例外拒绝",
                ),
                created_by="test",
            )

            denied = await service.evaluate(
                session,
                _request(fixture, fixture.member_one, "plugin.weather"),
            )
            allowed = await service.evaluate(
                session,
                _request(fixture, fixture.member_two, "plugin.weather"),
            )

        assert denied.effect is AclEffect.DENY
        assert denied.allowed is False
        assert allowed.effect is AclEffect.ALLOW
        assert allowed.allowed is True
    finally:
        await database.dispose()


async def test_member_exact_command_overrides_non_locked_group_rule(
    settings: Settings,
) -> None:
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with database.session_factory() as session:
            fixture = await _seed(session)
            service = PolicyService()
            for principal_id, effect, reason in (
                (None, AclEffect.DENY, "群默认拒绝命令"),
                (fixture.member_one.id, AclEffect.ALLOW, "仅允许指定成员"),
            ):
                await service.create_rule(
                    session,
                    _rule(
                        fixture,
                        principal_id=principal_id,
                        resource_type=AclResourceType.COMMAND,
                        resource_id="command.weather.query",
                        effect=effect,
                        reason=reason,
                    ),
                    created_by="test",
                )

            decision = await service.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=fixture.workspace_id,
                    bot_account_id=fixture.bot_account_id,
                    actor_principal_id=fixture.member_one.id,
                    chatroom_id=fixture.chatroom_id,
                    resource_type=AclResourceType.COMMAND,
                    resource_id="command.weather.query",
                    parent_plugin_id="plugin.weather",
                ),
            )

        assert decision.effect is AclEffect.ALLOW
    finally:
        await database.dispose()


async def test_locked_deny_and_membership_epoch_cannot_be_overridden(
    settings: Settings,
) -> None:
    database = Database(settings)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with database.session_factory() as session:
            fixture = await _seed(session)
            service = PolicyService()
            await service.create_rule(
                session,
                AclRuleCreate(
                    workspace_id=fixture.workspace_id,
                    scope_type=AclScopeType.WORKSPACE,
                    scope_id=str(fixture.workspace_id),
                    resource_type=AclResourceType.PLUGIN,
                    resource_id="plugin.weather",
                    effect=AclEffect.DENY,
                    locked=True,
                    reason="全局紧急停用",
                ),
                created_by="test",
            )
            await service.create_rule(
                session,
                _rule(
                    fixture,
                    principal_id=fixture.member_one.id,
                    resource_type=AclResourceType.COMMAND,
                    resource_id="command.weather.query",
                    effect=AclEffect.ALLOW,
                    reason="成员命令允许",
                ),
                created_by="test",
            )
            old_membership_rule = await service.create_rule(
                session,
                _rule(
                    fixture,
                    principal_id=fixture.member_one.id,
                    resource_type=AclResourceType.PLUGIN,
                    resource_id="plugin.other",
                    effect=AclEffect.ALLOW,
                    reason="旧成员关系允许",
                ),
                created_by="test",
            )

            locked = await service.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=fixture.workspace_id,
                    bot_account_id=fixture.bot_account_id,
                    actor_principal_id=fixture.member_one.id,
                    chatroom_id=fixture.chatroom_id,
                    resource_type=AclResourceType.COMMAND,
                    resource_id="command.weather.query",
                    parent_plugin_id="plugin.weather",
                ),
            )
            before_rejoin = await service.evaluate(
                session,
                _request(fixture, fixture.member_one, "plugin.other"),
            )
            directory = DirectoryService(cipher=CredentialCipher.from_settings(settings))
            membership = await session.scalar(
                select(ChatroomMembership).where(
                    ChatroomMembership.chatroom_id == fixture.chatroom_id,
                    ChatroomMembership.member_wxid == fixture.member_one.external_id,
                    ChatroomMembership.membership_epoch == 1,
                )
            )
            assert membership is not None
            closed_membership = await directory.mark_membership_left(
                session,
                fixture.chatroom_id,
                membership.id,
                membership_epoch=1,
                reason="Confirmed member departure",
                actor=AuthPrincipal(
                    user_id=uuid4(),
                    session_id=uuid4(),
                    username="policy-test-operator",
                    display_name=None,
                    roles=frozenset({"owner"}),
                    permissions=frozenset(),
                ),
            )
            after_leave = await service.evaluate(
                session,
                _request(fixture, fixture.member_one, "plugin.other"),
            )
            chatroom = await session.get(Chatroom, fixture.chatroom_id)
            assert chatroom is not None
            await directory._upsert_memberships(
                session,
                chatroom=chatroom,
                members_by_wxid={
                    fixture.member_one.external_id: ChatroomMember.model_validate(
                        {
                            "wxid": fixture.member_one.external_id,
                            "nickName": "Member One rejoined",
                            "inviterUserName": None,
                            "memberFlag": 1,
                            "displayName": "Member One",
                            "bigHeadImgUrl": "https://example.test/member-one-big.jpg",
                            "smallHeadImgUrl": "https://example.test/member-one-small.jpg",
                        }
                    )
                },
            )
            await session.flush()
            current_membership = await session.scalar(
                select(ChatroomMembership)
                .where(
                    ChatroomMembership.chatroom_id == fixture.chatroom_id,
                    ChatroomMembership.member_wxid == fixture.member_one.external_id,
                    ChatroomMembership.left_at.is_(None),
                )
                .order_by(ChatroomMembership.membership_epoch.desc())
            )
            after_rejoin = await service.evaluate(
                session,
                _request(fixture, fixture.member_one, "plugin.other"),
            )

        assert locked.effect is AclEffect.DENY
        assert locked.reason == "matched locked deny"
        assert before_rejoin.effect is AclEffect.ALLOW
        assert old_membership_rule.membership_epoch == 1
        assert closed_membership.membership_epoch == 1
        assert closed_membership.left_at is not None
        assert closed_membership.active is False
        assert after_leave.effect is AclEffect.DENY
        assert after_leave.reason == "no matching ACL rule; default deny"
        assert current_membership is not None
        assert current_membership.membership_epoch == 2
        assert after_rejoin.effect is AclEffect.DENY
        assert after_rejoin.reason == "no matching ACL rule; default deny"
    finally:
        await database.dispose()


async def _seed(session: AsyncSession) -> PolicyFixture:
    workspace = Workspace(name="Default", slug="default")
    session.add(workspace)
    await session.flush()
    connection = GeweConnection(
        workspace_id=workspace.id,
        name="Primary",
        api_base_url="https://api.gewe.test",
        token_ciphertext=b"encrypted",
        token_fingerprint="0123456789abcdef",
        callback_secret_ciphertext=b"encrypted",
        callback_secret_hash="a" * 64,
    )
    session.add(connection)
    await session.flush()
    account = BotAccount(
        gewe_connection_id=connection.id,
        app_id="app-1",
        wxid="wxid_bot",
        status=BotAccountStatus.ONLINE,
    )
    session.add(account)
    await session.flush()
    chatroom = Chatroom(
        bot_account_id=account.id,
        chatroom_id="room@chatroom",
        discovered_from="test",
    )
    session.add(chatroom)
    await session.flush()
    session.add_all(
        [
            ChatroomMembership(
                chatroom_id=chatroom.id,
                member_wxid="wxid_member_one",
                membership_epoch=1,
            ),
            ChatroomMembership(
                chatroom_id=chatroom.id,
                member_wxid="wxid_member_two",
                membership_epoch=1,
            ),
        ]
    )
    service = PolicyService()
    member_one = await service.create_principal(
        session,
        PrincipalCreate(
            workspace_id=workspace.id,
            principal_type=PrincipalType.GROUP_MEMBER,
            external_id="wxid_member_one",
            display_name="Member One",
        ),
    )
    member_two = await service.create_principal(
        session,
        PrincipalCreate(
            workspace_id=workspace.id,
            principal_type=PrincipalType.GROUP_MEMBER,
            external_id="wxid_member_two",
            display_name="Member Two",
        ),
    )
    await session.flush()
    return PolicyFixture(
        workspace_id=workspace.id,
        bot_account_id=account.id,
        chatroom_id=chatroom.id,
        member_one=member_one,
        member_two=member_two,
    )


def _rule(
    fixture: PolicyFixture,
    *,
    principal_id: UUID | None = None,
    resource_type: AclResourceType,
    resource_id: str,
    effect: AclEffect,
    reason: str,
) -> AclRuleCreate:
    return AclRuleCreate(
        workspace_id=fixture.workspace_id,
        principal_id=principal_id,
        scope_type=AclScopeType.CHATROOM,
        scope_id=str(fixture.chatroom_id),
        resource_type=resource_type,
        resource_id=resource_id,
        effect=effect,
        reason=reason,
    )


def _request(
    fixture: PolicyFixture,
    principal: Principal,
    resource_id: str,
) -> AclEvaluationRequest:
    return AclEvaluationRequest(
        workspace_id=fixture.workspace_id,
        bot_account_id=fixture.bot_account_id,
        actor_principal_id=principal.id,
        chatroom_id=fixture.chatroom_id,
        resource_type=AclResourceType.PLUGIN,
        resource_id=resource_id,
    )
