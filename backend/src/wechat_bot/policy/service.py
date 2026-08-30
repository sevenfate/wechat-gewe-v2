from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.db.base import utc_now
from wechat_bot.db.models import (
    BotAccount,
    BotAccountStatus,
    Chatroom,
    ChatroomMembership,
    Contact,
    GeweConnection,
    Workspace,
)
from wechat_bot.db.policy_models import (
    AclEffect,
    AclPolicyState,
    AclResourceType,
    AclRule,
    AclScopeType,
    PolicyDecision,
    Principal,
    PrincipalType,
)
from wechat_bot.policy.schemas import (
    AclDecisionView,
    AclEvaluationRequest,
    AclRuleCreate,
    PrincipalCreate,
)


class PolicyObjectNotFoundError(LookupError):
    pass


class InvalidPolicyRuleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Candidate:
    rule: AclRule
    resource_score: int
    subject_scope_score: int


class PolicyService:
    async def find_group_member_principal(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        chatroom_id: UUID,
        membership_id: UUID,
    ) -> Principal | None:
        membership = await self._active_group_membership(
            session,
            workspace_id=workspace_id,
            chatroom_id=chatroom_id,
            membership_id=membership_id,
        )
        principal: Principal | None = await session.scalar(
            select(Principal).where(
                Principal.workspace_id == workspace_id,
                Principal.principal_type == PrincipalType.GROUP_MEMBER,
                Principal.external_id == membership.member_wxid,
            )
        )
        return principal

    async def ensure_group_member_principal(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        chatroom_id: UUID,
        membership_id: UUID,
    ) -> Principal:
        membership = await self._active_group_membership(
            session,
            workspace_id=workspace_id,
            chatroom_id=chatroom_id,
            membership_id=membership_id,
        )
        return await self.create_principal(
            session,
            PrincipalCreate(
                workspace_id=workspace_id,
                principal_type=PrincipalType.GROUP_MEMBER,
                external_id=membership.member_wxid,
                display_name=membership.display_name or membership.nickname,
            ),
        )

    async def _active_group_membership(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        chatroom_id: UUID,
        membership_id: UUID,
    ) -> ChatroomMembership:
        membership: ChatroomMembership | None = await session.scalar(
            select(ChatroomMembership)
            .join(Chatroom, Chatroom.id == ChatroomMembership.chatroom_id)
            .join(BotAccount, BotAccount.id == Chatroom.bot_account_id)
            .join(GeweConnection, GeweConnection.id == BotAccount.gewe_connection_id)
            .where(
                GeweConnection.workspace_id == workspace_id,
                Chatroom.id == chatroom_id,
                ChatroomMembership.id == membership_id,
                ChatroomMembership.left_at.is_(None),
            )
        )
        if membership is None:
            raise PolicyObjectNotFoundError("active group membership not found in workspace")
        return membership

    async def create_principal(
        self,
        session: AsyncSession,
        payload: PrincipalCreate,
    ) -> Principal:
        if await session.get(Workspace, payload.workspace_id) is None:
            raise PolicyObjectNotFoundError("workspace not found")
        existing = await session.scalar(
            select(Principal).where(
                Principal.workspace_id == payload.workspace_id,
                Principal.principal_type == payload.principal_type,
                Principal.external_id == payload.external_id,
            )
        )
        if existing is not None:
            if payload.display_name is not None:
                existing.display_name = payload.display_name
            existing.active = True
            await session.flush()
            return existing
        principal = Principal(
            workspace_id=payload.workspace_id,
            principal_type=payload.principal_type,
            external_id=payload.external_id,
            display_name=payload.display_name,
        )
        session.add(principal)
        await session.flush()
        return principal

    async def create_rule(
        self,
        session: AsyncSession,
        payload: AclRuleCreate,
        *,
        created_by: str,
    ) -> AclRule:
        if await session.get(Workspace, payload.workspace_id) is None:
            raise PolicyObjectNotFoundError("workspace not found")
        principal = await self._validate_principal(session, payload)
        await self._validate_scope(session, payload)
        membership_epoch = await self._resolve_membership_epoch(
            session,
            payload,
            principal,
        )
        rule = AclRule(
            workspace_id=payload.workspace_id,
            principal_id=payload.principal_id,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            effect=payload.effect,
            locked=payload.locked,
            membership_epoch=membership_epoch,
            valid_from=payload.valid_from or utc_now(),
            valid_until=payload.valid_until,
            reason=payload.reason,
            created_by=created_by,
        )
        session.add(rule)
        await self._increment_version(session, payload.workspace_id)
        await session.flush()
        return rule

    async def revoke_rule(
        self,
        session: AsyncSession,
        rule_id: UUID,
        *,
        revoked_by: str,
    ) -> AclRule:
        rule = await session.get(AclRule, rule_id)
        if rule is None:
            raise PolicyObjectNotFoundError("ACL rule not found")
        if rule.revoked_at is None:
            rule.revoked_at = utc_now()
            rule.revoked_by = revoked_by
            await self._increment_version(session, rule.workspace_id)
            await session.flush()
        return rule

    async def list_rules(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID | None = None,
        scope_type: AclScopeType | None = None,
        scope_id: str | None = None,
        include_revoked: bool = False,
    ) -> tuple[list[AclRule], int]:
        filters = []
        if workspace_id is not None:
            filters.append(AclRule.workspace_id == workspace_id)
        if scope_type is not None:
            filters.append(AclRule.scope_type == scope_type)
        if scope_id is not None:
            filters.append(AclRule.scope_id == scope_id)
        if not include_revoked:
            filters.append(AclRule.revoked_at.is_(None))
        statement = (
            select(AclRule).where(*filters).order_by(AclRule.created_at.desc(), AclRule.id.desc())
        )
        items = list(await session.scalars(statement))
        total = await session.scalar(select(func.count()).select_from(AclRule).where(*filters))
        return items, total or 0

    async def evaluate(
        self,
        session: AsyncSession,
        request: AclEvaluationRequest,
    ) -> AclDecisionView:
        now = utc_now()
        policy_version = await self._policy_version(session, request.workspace_id)
        account = await session.scalar(
            select(BotAccount)
            .join(GeweConnection, BotAccount.gewe_connection_id == GeweConnection.id)
            .where(
                BotAccount.id == request.bot_account_id,
                GeweConnection.workspace_id == request.workspace_id,
            )
        )
        if account is None:
            raise PolicyObjectNotFoundError("bot account not found in workspace")
        if account.status is BotAccountStatus.DISABLED:
            return await self._record_decision(
                session,
                request,
                effect=AclEffect.DENY,
                reason="bot account is disabled",
                policy_version=policy_version,
                matched=[],
            )

        principal = await self._active_request_principal(session, request)
        membership_epoch = await self._current_membership_epoch(session, request, principal)
        rules = list(
            await session.scalars(
                select(AclRule).where(
                    AclRule.workspace_id == request.workspace_id,
                    AclRule.revoked_at.is_(None),
                    AclRule.valid_from <= now,
                    or_(AclRule.valid_until.is_(None), AclRule.valid_until > now),
                    or_(
                        AclRule.principal_id.is_(None),
                        AclRule.principal_id == request.actor_principal_id,
                    ),
                )
            )
        )
        candidates = self._candidates(
            rules,
            request=request,
            principal=principal,
            membership_epoch=membership_epoch,
        )
        locked_denies = [item for item in candidates if item.rule.locked]
        if locked_denies:
            return await self._record_decision(
                session,
                request,
                effect=AclEffect.DENY,
                reason="matched locked deny",
                policy_version=policy_version,
                matched=[item.rule for item in locked_denies],
            )
        if not candidates:
            return await self._record_decision(
                session,
                request,
                effect=AclEffect.DENY,
                reason="no matching ACL rule; default deny",
                policy_version=policy_version,
                matched=[],
            )

        best_score = max((item.resource_score, item.subject_scope_score) for item in candidates)
        best = [
            item
            for item in candidates
            if (item.resource_score, item.subject_scope_score) == best_score
        ]
        effect = max(
            (item.rule.effect for item in best),
            key={AclEffect.ALLOW: 1, AclEffect.ASK: 2, AclEffect.DENY: 3}.__getitem__,
        )
        return await self._record_decision(
            session,
            request,
            effect=effect,
            reason=f"matched highest-specificity {effect.value} rule",
            policy_version=policy_version,
            matched=[item.rule for item in best],
        )

    def _candidates(
        self,
        rules: list[AclRule],
        *,
        request: AclEvaluationRequest,
        principal: Principal | None,
        membership_epoch: int | None,
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for rule in rules:
            if rule.principal_id is not None:
                if principal is None or rule.principal_id != principal.id:
                    continue
                if principal.principal_type is PrincipalType.GROUP_MEMBER and (
                    membership_epoch is None
                    or rule.membership_epoch is None
                    or rule.membership_epoch != membership_epoch
                ):
                    continue
            resource_score = self._resource_score(rule, request)
            scope_score = self._scope_score(rule, request)
            if resource_score == 0 or scope_score == 0:
                continue
            if rule.principal_id is not None:
                scope_score = 40
            candidates.append(
                _Candidate(
                    rule=rule,
                    resource_score=resource_score,
                    subject_scope_score=scope_score,
                )
            )
        return candidates

    @staticmethod
    def _resource_score(rule: AclRule, request: AclEvaluationRequest) -> int:
        if rule.resource_type == request.resource_type and rule.resource_id == request.resource_id:
            return 30
        if (
            rule.resource_type is AclResourceType.PLUGIN
            and request.parent_plugin_id is not None
            and rule.resource_id == request.parent_plugin_id
        ):
            return 20
        if rule.resource_type is AclResourceType.CATEGORY and rule.resource_id == "*":
            return 10
        return 0

    @staticmethod
    def _scope_score(rule: AclRule, request: AclEvaluationRequest) -> int:
        expected: dict[AclScopeType, tuple[str | None, int]] = {
            AclScopeType.WORKSPACE: (str(request.workspace_id), 10),
            AclScopeType.BOT_ACCOUNT: (str(request.bot_account_id), 20),
            AclScopeType.CHATROOM: (
                str(request.chatroom_id) if request.chatroom_id is not None else None,
                30,
            ),
            AclScopeType.CONTACT: (
                str(request.contact_id) if request.contact_id is not None else None,
                40,
            ),
        }
        expected_id, score = expected[rule.scope_type]
        return score if expected_id is not None and rule.scope_id == expected_id else 0

    async def _validate_principal(
        self,
        session: AsyncSession,
        payload: AclRuleCreate,
    ) -> Principal | None:
        if payload.principal_id is None:
            if payload.membership_epoch is not None:
                raise InvalidPolicyRuleError("membership_epoch requires a group member principal")
            return None
        principal = await session.get(Principal, payload.principal_id)
        if principal is None or principal.workspace_id != payload.workspace_id:
            raise PolicyObjectNotFoundError("principal not found in workspace")
        if not principal.active:
            raise InvalidPolicyRuleError("principal is inactive")
        return principal

    async def _validate_scope(
        self,
        session: AsyncSession,
        payload: AclRuleCreate,
    ) -> None:
        if payload.scope_type is AclScopeType.WORKSPACE:
            if payload.scope_id != str(payload.workspace_id):
                raise InvalidPolicyRuleError("workspace scope_id does not match workspace")
            return
        scope_uuid = _parse_uuid(payload.scope_id, "scope_id must be a UUID")
        if payload.scope_type is AclScopeType.BOT_ACCOUNT:
            exists = await session.scalar(
                select(BotAccount.id)
                .join(GeweConnection, BotAccount.gewe_connection_id == GeweConnection.id)
                .where(
                    BotAccount.id == scope_uuid,
                    GeweConnection.workspace_id == payload.workspace_id,
                )
            )
        elif payload.scope_type is AclScopeType.CHATROOM:
            exists = await session.scalar(
                select(Chatroom.id)
                .join(BotAccount, Chatroom.bot_account_id == BotAccount.id)
                .join(GeweConnection, BotAccount.gewe_connection_id == GeweConnection.id)
                .where(
                    Chatroom.id == scope_uuid,
                    GeweConnection.workspace_id == payload.workspace_id,
                )
            )
        else:
            exists = await session.scalar(
                select(Contact.id)
                .join(BotAccount, Contact.bot_account_id == BotAccount.id)
                .join(GeweConnection, BotAccount.gewe_connection_id == GeweConnection.id)
                .where(
                    Contact.id == scope_uuid,
                    GeweConnection.workspace_id == payload.workspace_id,
                )
            )
        if exists is None:
            raise PolicyObjectNotFoundError("ACL scope not found in workspace")

    async def _resolve_membership_epoch(
        self,
        session: AsyncSession,
        payload: AclRuleCreate,
        principal: Principal | None,
    ) -> int | None:
        if principal is None or principal.principal_type is not PrincipalType.GROUP_MEMBER:
            if payload.membership_epoch is not None:
                raise InvalidPolicyRuleError("membership_epoch is only valid for group members")
            return None
        if payload.scope_type is not AclScopeType.CHATROOM:
            raise InvalidPolicyRuleError("group member rules require CHATROOM scope")
        chatroom_id = _parse_uuid(payload.scope_id, "chatroom scope_id must be a UUID")
        membership = await session.scalar(
            select(ChatroomMembership)
            .where(
                ChatroomMembership.chatroom_id == chatroom_id,
                ChatroomMembership.member_wxid == principal.external_id,
                ChatroomMembership.left_at.is_(None),
            )
            .order_by(ChatroomMembership.membership_epoch.desc())
        )
        if membership is None:
            raise InvalidPolicyRuleError("principal is not an active member of the chatroom")
        if (
            payload.membership_epoch is not None
            and payload.membership_epoch != membership.membership_epoch
        ):
            raise InvalidPolicyRuleError("membership_epoch is stale")
        return membership.membership_epoch

    async def _active_request_principal(
        self,
        session: AsyncSession,
        request: AclEvaluationRequest,
    ) -> Principal | None:
        if request.actor_principal_id is None:
            return None
        principal = await session.get(Principal, request.actor_principal_id)
        if (
            principal is None
            or principal.workspace_id != request.workspace_id
            or not principal.active
        ):
            raise InvalidPolicyRuleError("request principal is missing or inactive")
        return principal

    async def _current_membership_epoch(
        self,
        session: AsyncSession,
        request: AclEvaluationRequest,
        principal: Principal | None,
    ) -> int | None:
        if principal is None or principal.principal_type is not PrincipalType.GROUP_MEMBER:
            return None
        if request.chatroom_id is None:
            return None
        epoch = await session.scalar(
            select(ChatroomMembership.membership_epoch)
            .where(
                ChatroomMembership.chatroom_id == request.chatroom_id,
                ChatroomMembership.member_wxid == principal.external_id,
                ChatroomMembership.left_at.is_(None),
            )
            .order_by(ChatroomMembership.membership_epoch.desc())
        )
        return int(epoch) if epoch is not None else None

    async def _increment_version(self, session: AsyncSession, workspace_id: UUID) -> int:
        state = await session.scalar(
            select(AclPolicyState)
            .where(AclPolicyState.workspace_id == workspace_id)
            .with_for_update()
        )
        if state is None:
            state = AclPolicyState(workspace_id=workspace_id, version=1)
            session.add(state)
        else:
            state.version += 1
        await session.flush()
        return state.version

    async def _policy_version(self, session: AsyncSession, workspace_id: UUID) -> int:
        version = await session.scalar(
            select(AclPolicyState.version).where(AclPolicyState.workspace_id == workspace_id)
        )
        return version or 0

    async def _record_decision(
        self,
        session: AsyncSession,
        request: AclEvaluationRequest,
        *,
        effect: AclEffect,
        reason: str,
        policy_version: int,
        matched: list[AclRule],
    ) -> AclDecisionView:
        matched_ids = [rule.id for rule in matched]
        session.add(
            PolicyDecision(
                workspace_id=request.workspace_id,
                trace_id=request.trace_id,
                policy_version=policy_version,
                effect=effect,
                reason=reason,
                request_snapshot={
                    "bot_account_id": str(request.bot_account_id),
                    "actor_principal_id": (
                        str(request.actor_principal_id)
                        if request.actor_principal_id is not None
                        else None
                    ),
                    "chatroom_id": (
                        str(request.chatroom_id) if request.chatroom_id is not None else None
                    ),
                    "contact_id": (
                        str(request.contact_id) if request.contact_id is not None else None
                    ),
                    "resource_type": request.resource_type.value,
                    "resource_id": request.resource_id,
                    "parent_plugin_id": request.parent_plugin_id,
                },
                matched_rule_ids=[str(rule_id) for rule_id in matched_ids],
            )
        )
        await session.flush()
        return AclDecisionView(
            effect=effect,
            allowed=effect is AclEffect.ALLOW,
            reason=reason,
            policy_version=policy_version,
            matched_rule_ids=matched_ids,
        )


def _parse_uuid(value: str, error_message: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise InvalidPolicyRuleError(error_message) from exc
