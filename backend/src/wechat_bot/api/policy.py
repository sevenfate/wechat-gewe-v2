from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from wechat_bot.api.dependencies import get_session
from wechat_bot.auth.dependencies import require_management_request, require_permission
from wechat_bot.db.policy_models import AclScopeType
from wechat_bot.policy.schemas import (
    AclDecisionView,
    AclEvaluationRequest,
    AclRuleCreate,
    AclRuleList,
    AclRuleView,
    GroupMemberPrincipalEnsure,
    GroupMemberPrincipalLookup,
    PrincipalView,
)
from wechat_bot.policy.service import (
    InvalidPolicyRuleError,
    PolicyObjectNotFoundError,
    PolicyService,
)

router = APIRouter(
    prefix="/api/v1/policy",
    tags=["Runtime policy"],
    dependencies=[
        Depends(require_management_request),
        Depends(require_permission("policy.read")),
    ],
)


@router.get(
    "/principals/group-member",
    response_model=GroupMemberPrincipalLookup,
    dependencies=[Depends(require_permission("directory.read"))],
)
async def find_group_member_principal(
    workspace_id: UUID,
    chatroom_id: UUID,
    membership_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GroupMemberPrincipalLookup:
    try:
        principal = await PolicyService().find_group_member_principal(
            session,
            workspace_id=workspace_id,
            chatroom_id=chatroom_id,
            membership_id=membership_id,
        )
    except PolicyObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    return GroupMemberPrincipalLookup(
        workspace_id=workspace_id,
        chatroom_id=chatroom_id,
        membership_id=membership_id,
        principal=PrincipalView.model_validate(principal) if principal is not None else None,
    )


@router.post(
    "/principals/group-member",
    response_model=PrincipalView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_permission("directory.read")),
        Depends(require_permission("policy.write")),
    ],
)
async def ensure_group_member_principal(
    payload: GroupMemberPrincipalEnsure,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PrincipalView:
    try:
        principal = await PolicyService().ensure_group_member_principal(
            session,
            workspace_id=payload.workspace_id,
            chatroom_id=payload.chatroom_id,
            membership_id=payload.membership_id,
        )
    except PolicyObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    await session.commit()
    return PrincipalView.model_validate(principal)


@router.post(
    "/rules",
    response_model=AclRuleView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission("policy.write"))],
)
async def create_rule(
    payload: AclRuleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AclRuleView:
    try:
        rule = await PolicyService().create_rule(session, payload, created_by="api")
    except PolicyObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except InvalidPolicyRuleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return AclRuleView.model_validate(rule)


@router.get("/rules", response_model=AclRuleList)
async def list_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
    workspace_id: UUID | None = None,
    scope_type: AclScopeType | None = None,
    scope_id: str | None = None,
    include_revoked: Annotated[bool, Query()] = False,
) -> AclRuleList:
    items, total = await PolicyService().list_rules(
        session,
        workspace_id=workspace_id,
        scope_type=scope_type,
        scope_id=scope_id,
        include_revoked=include_revoked,
    )
    return AclRuleList(
        items=[AclRuleView.model_validate(item) for item in items],
        total=total,
    )


@router.post(
    "/rules/{rule_id}/revoke",
    response_model=AclRuleView,
    dependencies=[Depends(require_permission("policy.write"))],
)
async def revoke_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AclRuleView:
    try:
        rule = await PolicyService().revoke_rule(session, rule_id, revoked_by="api")
    except PolicyObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    await session.commit()
    return AclRuleView.model_validate(rule)


@router.post(
    "/evaluate",
    response_model=AclDecisionView,
    dependencies=[Depends(require_permission("policy.evaluate"))],
)
async def evaluate_policy(
    payload: AclEvaluationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AclDecisionView:
    try:
        result = await PolicyService().evaluate(session, payload)
    except PolicyObjectNotFoundError as exc:
        raise _not_found(exc) from exc
    except InvalidPolicyRuleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return result


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
