from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from wechat_bot.core.crypto import CredentialCipher, CredentialDecryptionError
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
    Chatroom,
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
    PluginRevisionActivation,
)
from wechat_bot.db.policy_models import AclResourceType, Principal, PrincipalType
from wechat_bot.maibot.constants import (
    MAIBOT_API_KEY_PLACEHOLDER,
    MAIBOT_CONNECTOR_PLUGIN_ID,
    MAIBOT_FORWARD_CAPABILITY,
    MAIBOT_PROACTIVE_CAPABILITY,
)
from wechat_bot.maibot.mapping import (
    MaiBotInboundText,
    MaiBotOutboundText,
    MaiBotProtocolError,
    build_inbound_text_envelope,
    parse_outbound_text_envelope,
)
from wechat_bot.maibot.schemas import (
    MaiBotActivationContext,
    MaiBotConnectorConfig,
    MaiBotConversationContextClaims,
    MaiBotEventSubmission,
)
from wechat_bot.outbox.schemas import OutboxAuthorizationContext
from wechat_bot.outbox.service import (
    TEXT_ACTION_TYPE,
    TEXT_REPLY_ACTION_TYPE,
    OutboxIdempotencyConflictError,
    OutboxService,
)
from wechat_bot.policy.schemas import AclEvaluationRequest, PrincipalCreate
from wechat_bot.policy.service import (
    InvalidPolicyRuleError,
    PolicyObjectNotFoundError,
    PolicyService,
)

_DELIVERABLE_STATUSES = frozenset(
    {
        MaiBotBridgeStatus.PENDING,
        MaiBotBridgeStatus.CLAIMED,
        MaiBotBridgeStatus.SENT,
        MaiBotBridgeStatus.FAILED_RETRYABLE,
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedConversationContext:
    source: MaiBotBridgeEnvelope
    account: BotAccount
    chatroom: Chatroom | None
    contact: Contact | None
    authorization: OutboxAuthorizationContext


class MaiBotBridgeError(RuntimeError):
    pass


class MaiBotEnvelopeConflictError(MaiBotBridgeError):
    pass


class MaiBotStaleActivationError(MaiBotBridgeError):
    pass


class MaiBotBridgeService:
    def __init__(
        self,
        cipher: CredentialCipher,
        *,
        policy_service: PolicyService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._cipher = cipher
        self._policy = policy_service or PolicyService()
        self._outbox = outbox_service or OutboxService()

    async def enqueue_event(
        self,
        session: AsyncSession,
        submission: MaiBotEventSubmission,
    ) -> MaiBotBridgeEnvelope | None:
        revision = await session.get(PluginDeploymentRevision, submission.deployment_revision_id)
        if revision is None or revision.deployment_id != submission.deployment_id:
            return None
        try:
            config = self._revision_config(revision)
        except MaiBotBridgeError:
            return None
        if MAIBOT_FORWARD_CAPABILITY not in revision.grants:
            return None
        activation = await self._active_activation(
            session,
            deployment_id=submission.deployment_id,
            revision_id=submission.deployment_revision_id,
        )
        if activation is None:
            return None
        pending_count = await session.scalar(
            select(func.count())
            .select_from(MaiBotBridgeEnvelope)
            .where(
                MaiBotBridgeEnvelope.deployment_id == submission.deployment_id,
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT,
                MaiBotBridgeEnvelope.status.in_(_DELIVERABLE_STATUSES),
            )
        )
        if (pending_count or 0) >= config.max_pending_messages:
            return None

        existing = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.deployment_id == submission.deployment_id,
                MaiBotBridgeEnvelope.source_event_id == submission.event_id,
            )
        )
        if existing is not None:
            return existing

        now = utc_now()
        envelope_id = f"maibot:{submission.deployment_id}:{submission.event_id}"
        row_id = uuid7()
        envelope = build_inbound_text_envelope(
            MaiBotInboundText(
                envelope_id=envelope_id,
                business_message_id=submission.business_message_id,
                timestamp=submission.occurred_at.timestamp(),
                actor_id=submission.actor_wxid,
                actor_nickname=submission.actor_nickname,
                actor_cardname=submission.actor_cardname,
                group_id=(
                    submission.conversation_external_id
                    if submission.conversation_type.value == "GROUP"
                    else None
                ),
                group_name=(
                    submission.group_name if submission.conversation_type.value == "GROUP" else None
                ),
                text=submission.text,
                connector_context_id=self._issue_conversation_context(row_id),
                platform_account_id=submission.bot_app_id,
                platform_scope=str(submission.deployment_id),
            )
        )
        payload_sha256 = _json_hash(envelope)
        row = MaiBotBridgeEnvelope(
            id=row_id,
            deployment_id=submission.deployment_id,
            deployment_revision_id=submission.deployment_revision_id,
            activation_id=activation.id,
            bot_account_id=submission.bot_account_id,
            trace_id=submission.trace_id,
            direction=MaiBotBridgeDirection.TO_MAIBOT,
            kind=MaiBotBridgeKind.MESSAGE,
            transport_message_id=envelope_id,
            business_message_id=submission.business_message_id,
            source_event_id=submission.event_id,
            actor_principal_id=submission.authorization_context.actor_principal_id,
            chatroom_id=submission.authorization_context.chatroom_id,
            contact_id=submission.authorization_context.contact_id,
            target_wxid=submission.conversation_external_id,
            authorization_context=submission.authorization_context.model_dump(mode="json"),
            envelope=envelope,
            payload_sha256=payload_sha256,
            status=MaiBotBridgeStatus.PENDING,
            expires_at=now + timedelta(seconds=config.message_ttl_seconds),
            available_at=now,
            attempt_count=0,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raced = await session.scalar(
                select(MaiBotBridgeEnvelope).where(
                    MaiBotBridgeEnvelope.deployment_id == submission.deployment_id,
                    MaiBotBridgeEnvelope.source_event_id == submission.event_id,
                )
            )
            if raced is None:
                raise
            if raced.payload_sha256 != payload_sha256:
                raise MaiBotEnvelopeConflictError(
                    "source event is already bound to a different MaiBot envelope"
                ) from exc
            return raced
        return row

    async def activation_context(
        self,
        session: AsyncSession,
        *,
        deployment_id: UUID,
        activation_epoch: int,
    ) -> MaiBotActivationContext | None:
        row = (
            await session.execute(
                select(
                    PluginDeployment,
                    Plugin,
                    PluginDeploymentRevision,
                    PluginRevisionActivation,
                )
                .join(Plugin, Plugin.id == PluginDeployment.plugin_id)
                .join(
                    PluginDeploymentRevision,
                    PluginDeploymentRevision.id == PluginDeployment.active_revision_id,
                )
                .join(
                    PluginRevisionActivation,
                    PluginRevisionActivation.revision_id == PluginDeploymentRevision.id,
                )
                .where(
                    PluginDeployment.id == deployment_id,
                    PluginDeployment.status == PluginDeploymentStatus.RUNNING,
                    PluginRevisionActivation.deployment_id == deployment_id,
                    PluginRevisionActivation.activation_epoch == activation_epoch,
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        deployment, plugin, revision, activation = row
        if plugin.plugin_id != MAIBOT_CONNECTOR_PLUGIN_ID:
            return None
        return MaiBotActivationContext(
            deployment_id=deployment.id,
            deployment_revision_id=revision.id,
            activation_id=activation.id,
            activation_epoch=activation.activation_epoch,
            fencing_token=activation.fencing_token,
            workspace_id=deployment.workspace_id,
            plugin_id=plugin.plugin_id,
            revision_grants=frozenset(revision.grants),
            revision_scope=revision.scope,
        )

    async def claim_next(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        lease_seconds: float = 30.0,
    ) -> MaiBotBridgeEnvelope | None:
        if not await self._context_is_current(session, context, lock=True):
            return None
        now = utc_now()
        stale = list(
            await session.scalars(
                select(MaiBotBridgeEnvelope)
                .where(
                    MaiBotBridgeEnvelope.deployment_id == context.deployment_id,
                    MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT,
                    MaiBotBridgeEnvelope.status.in_(_DELIVERABLE_STATUSES),
                    MaiBotBridgeEnvelope.activation_id != context.activation_id,
                )
                .with_for_update(skip_locked=True)
            )
        )
        for item in stale:
            item.status = MaiBotBridgeStatus.CANCELLED
            item.completed_at = now
            item.last_error_code = "MAIBOT_FENCE_REVOKED"

        candidate = await session.scalar(
            select(MaiBotBridgeEnvelope)
            .where(
                MaiBotBridgeEnvelope.deployment_id == context.deployment_id,
                MaiBotBridgeEnvelope.activation_id == context.activation_id,
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT,
                MaiBotBridgeEnvelope.status.in_(_DELIVERABLE_STATUSES),
            )
            .order_by(MaiBotBridgeEnvelope.created_at, MaiBotBridgeEnvelope.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            await session.flush()
            return None
        if _as_utc(candidate.expires_at) <= now:
            candidate.status = MaiBotBridgeStatus.EXPIRED
            candidate.completed_at = now
            candidate.last_error_code = "MAIBOT_CONTEXT_EXPIRED"
            await session.flush()
            return None
        if _as_utc(candidate.available_at) > now:
            await session.flush()
            return None
        if MAIBOT_FORWARD_CAPABILITY not in context.revision_grants:
            candidate.status = MaiBotBridgeStatus.CANCELLED
            candidate.completed_at = now
            candidate.last_error_code = "MAIBOT_FORWARD_GRANT_REVOKED"
            await session.flush()
            return None
        if not await self._authorization_allowed(session, candidate):
            candidate.status = MaiBotBridgeStatus.CANCELLED
            candidate.completed_at = now
            candidate.last_error_code = "MAIBOT_POLICY_CHANGED"
            await session.flush()
            return None
        candidate.status = MaiBotBridgeStatus.CLAIMED
        candidate.available_at = now + timedelta(seconds=lease_seconds)
        candidate.attempt_count += 1
        candidate.last_error_code = None
        await session.flush()
        return candidate

    async def mark_sent(
        self,
        session: AsyncSession,
        envelope_id: UUID,
        *,
        ack_retry_seconds: float,
    ) -> None:
        row = await session.get(MaiBotBridgeEnvelope, envelope_id, with_for_update=True)
        if row is None or row.status is not MaiBotBridgeStatus.CLAIMED:
            return
        row.status = MaiBotBridgeStatus.SENT
        row.available_at = utc_now() + timedelta(seconds=ack_retry_seconds)
        await session.flush()

    async def mark_retryable(
        self,
        session: AsyncSession,
        envelope_id: UUID,
        *,
        error_code: str,
        retry_seconds: float,
    ) -> None:
        row = await session.get(MaiBotBridgeEnvelope, envelope_id, with_for_update=True)
        if row is None or row.status not in {
            MaiBotBridgeStatus.CLAIMED,
            MaiBotBridgeStatus.SENT,
        }:
            return
        row.status = MaiBotBridgeStatus.FAILED_RETRYABLE
        row.available_at = utc_now() + timedelta(seconds=retry_seconds)
        row.last_error_code = error_code[:100]
        await session.flush()

    async def acknowledge(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        transport_message_id: str,
    ) -> MaiBotBridgeEnvelope | None:
        if not await self._context_is_current(session, context):
            return None
        row = await session.scalar(
            select(MaiBotBridgeEnvelope)
            .where(
                MaiBotBridgeEnvelope.deployment_id == context.deployment_id,
                MaiBotBridgeEnvelope.activation_id == context.activation_id,
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.TO_MAIBOT,
                MaiBotBridgeEnvelope.transport_message_id == transport_message_id,
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.status in {
            MaiBotBridgeStatus.CLAIMED,
            MaiBotBridgeStatus.SENT,
            MaiBotBridgeStatus.FAILED_RETRYABLE,
        }:
            now = utc_now()
            row.status = MaiBotBridgeStatus.ACKED
            row.acked_at = now
            row.completed_at = now
            row.last_error_code = None
            await session.flush()
        return row

    async def receive_standard(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        config: MaiBotConnectorConfig,
        envelope: Mapping[str, Any],
    ) -> MaiBotBridgeEnvelope:
        if not await self._context_is_current(session, context, lock=True):
            raise MaiBotStaleActivationError("MaiBot connector activation is no longer current")
        sanitized = _sanitize_envelope(envelope)
        payload_sha256 = _json_hash(sanitized)
        raw_transport_id = envelope.get("msg_id")
        transport_id = (
            raw_transport_id.strip()
            if isinstance(raw_transport_id, str) and raw_transport_id.strip()
            else f"invalid:{payload_sha256[:32]}"
        )[:255]
        existing = await session.scalar(
            select(MaiBotBridgeEnvelope).where(
                MaiBotBridgeEnvelope.deployment_id == context.deployment_id,
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.FROM_MAIBOT,
                MaiBotBridgeEnvelope.transport_message_id == transport_id,
            )
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha256:
                raise MaiBotEnvelopeConflictError(
                    "MaiBot transport message id was reused with a different payload"
                )
            return existing

        now = utc_now()
        row = MaiBotBridgeEnvelope(
            deployment_id=context.deployment_id,
            deployment_revision_id=context.deployment_revision_id,
            activation_id=context.activation_id,
            trace_id=uuid7(),
            direction=MaiBotBridgeDirection.FROM_MAIBOT,
            kind=MaiBotBridgeKind.PROACTIVE,
            transport_message_id=transport_id[:255],
            envelope=sanitized,
            payload_sha256=payload_sha256,
            authorization_context={},
            status=MaiBotBridgeStatus.RECEIVED,
            expires_at=now + timedelta(seconds=config.message_ttl_seconds),
            available_at=now,
            attempt_count=1,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError as exc:
            raced = await session.scalar(
                select(MaiBotBridgeEnvelope).where(
                    MaiBotBridgeEnvelope.deployment_id == context.deployment_id,
                    MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.FROM_MAIBOT,
                    MaiBotBridgeEnvelope.transport_message_id == transport_id,
                )
            )
            if raced is None:
                raise
            if raced.payload_sha256 != payload_sha256:
                raise MaiBotEnvelopeConflictError(
                    "MaiBot transport message id was reused with a different payload"
                ) from exc
            return raced
        try:
            intent = parse_outbound_text_envelope(
                envelope,
                expected_api_key=config.api_key.get_secret_value(),
                allow_group_with_user=True,
            )
        except MaiBotProtocolError:
            self._reject(row, "MAIBOT_INVALID_ENVELOPE")
            await session.flush()
            return row

        connector_context_id = _connector_context_id(envelope)
        if connector_context_id is None:
            self._reject(row, "MAIBOT_CONTEXT_REQUIRED")
            await session.flush()
            return row

        row.business_message_id = intent.business_message_id
        row.reply_to_business_message_id = intent.reply_to_business_message_id
        row.target_wxid = intent.target_wxid
        if intent.reply_to_business_message_id is not None:
            row.kind = MaiBotBridgeKind.REPLY
            await self._accept_reply(
                session,
                context=context,
                row=row,
                intent=intent,
                connector_context_id=connector_context_id,
            )
        else:
            row.kind = MaiBotBridgeKind.PROACTIVE
            await self._accept_proactive(
                session,
                context=context,
                config=config,
                row=row,
                intent=intent,
                connector_context_id=connector_context_id,
            )
        await session.flush()
        return row

    async def set_connection_status(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        status: MaiBotConnectionStatus,
        error_code: str | None = None,
    ) -> MaiBotConnectionState | None:
        if not await self._context_is_current(session, context, lock=True):
            return None
        state = await session.scalar(
            select(MaiBotConnectionState)
            .where(MaiBotConnectionState.deployment_id == context.deployment_id)
            .with_for_update()
        )
        now = utc_now()
        if state is None:
            candidate = MaiBotConnectionState(
                deployment_id=context.deployment_id,
                deployment_revision_id=context.deployment_revision_id,
                activation_id=context.activation_id,
                status=status,
            )
            try:
                async with session.begin_nested():
                    session.add(candidate)
                    await session.flush()
                state = candidate
            except IntegrityError:
                state = await session.scalar(
                    select(MaiBotConnectionState)
                    .where(MaiBotConnectionState.deployment_id == context.deployment_id)
                    .with_for_update()
                )
                if state is None:
                    raise
        else:
            state.deployment_revision_id = context.deployment_revision_id
            state.activation_id = context.activation_id
            state.status = status
        if status is MaiBotConnectionStatus.CONNECTED:
            state.connected_at = now
            state.last_error_code = None
        elif status in {MaiBotConnectionStatus.BACKOFF, MaiBotConnectionStatus.STOPPED}:
            state.disconnected_at = now
            state.last_error_code = error_code[:100] if error_code else None
        await session.flush()
        return state

    async def _context_is_current(
        self,
        session: AsyncSession,
        context: MaiBotActivationContext,
        *,
        lock: bool = False,
    ) -> bool:
        if lock:
            locked = await session.scalar(
                select(PluginDeployment.id)
                .where(PluginDeployment.id == context.deployment_id)
                .with_for_update()
            )
            if locked is None:
                return False
        current = await self.activation_context(
            session,
            deployment_id=context.deployment_id,
            activation_epoch=context.activation_epoch,
        )
        return current == context

    async def _resolve_conversation_context(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        row: MaiBotBridgeEnvelope,
        connector_context_id: str,
    ) -> _ResolvedConversationContext | None:
        try:
            claims = MaiBotConversationContextClaims.model_validate_json(
                self._cipher.decrypt(connector_context_id.encode("ascii"))
            )
        except (CredentialDecryptionError, UnicodeEncodeError, ValidationError):
            self._reject(row, "MAIBOT_CONTEXT_INVALID")
            return None

        source = await session.scalar(
            select(MaiBotBridgeEnvelope)
            .where(MaiBotBridgeEnvelope.id == claims.source_envelope_id)
            .with_for_update()
        )
        now = utc_now()
        if source is None:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_NOT_FOUND")
            return None
        if (
            source.direction is not MaiBotBridgeDirection.TO_MAIBOT
            or source.kind is not MaiBotBridgeKind.MESSAGE
            or source.deployment_id != context.deployment_id
        ):
            self._reject(row, "MAIBOT_CONTEXT_SCOPE_MISMATCH")
            return None
        if (
            source.activation_id != context.activation_id
            or source.deployment_revision_id != context.deployment_revision_id
        ):
            self._reject(row, "MAIBOT_STALE_SOURCE_CONTEXT")
            return None
        if source.status not in {
            MaiBotBridgeStatus.SENT,
            MaiBotBridgeStatus.ACKED,
        }:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_NOT_DELIVERED")
            return None
        if _as_utc(source.expires_at) <= now:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_EXPIRED")
            return None
        already_consumed = await session.scalar(
            select(MaiBotBridgeEnvelope.id)
            .where(
                MaiBotBridgeEnvelope.direction == MaiBotBridgeDirection.FROM_MAIBOT,
                MaiBotBridgeEnvelope.source_envelope_id == source.id,
                MaiBotBridgeEnvelope.status == MaiBotBridgeStatus.ACCEPTED,
                MaiBotBridgeEnvelope.id != row.id,
            )
            .limit(1)
        )
        if already_consumed is not None:
            self._reject(row, "MAIBOT_CONTEXT_ALREADY_USED")
            return None

        try:
            authorization = self._authorization_context(source)
        except ValidationError:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return None
        if (
            authorization.workspace_id != context.workspace_id
            or authorization.deployment_id != context.deployment_id
            or authorization.deployment_revision_id != context.deployment_revision_id
            or authorization.actor_principal_id is None
            or authorization.actor_principal_id != source.actor_principal_id
            or authorization.chatroom_id != source.chatroom_id
            or authorization.contact_id != source.contact_id
            or source.bot_account_id is None
            or source.source_event_id is None
            or source.target_wxid is None
        ):
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return None

        account = await session.get(BotAccount, source.bot_account_id)
        if account is None:
            self._reject(row, "MAIBOT_SOURCE_ACCOUNT_NOT_FOUND")
            return None
        connection = await session.get(GeweConnection, account.gewe_connection_id)
        event = await session.get(NormalizedEvent, source.source_event_id)
        principal = await session.get(Principal, authorization.actor_principal_id)
        if event is not None:
            inbox = await session.get(WebhookInbox, event.webhook_inbox_id)
        else:
            inbox = None
        if (
            connection is None
            or connection.workspace_id != context.workspace_id
            or event is None
            or event.bot_account_id != account.id
            or event.conversation_id != source.target_wxid
            or event.actor_wxid is None
            or inbox is None
            or inbox.gewe_connection_id != connection.id
            or inbox.app_id != account.app_id
            or inbox.trace_id != source.trace_id
            or principal is None
            or principal.workspace_id != context.workspace_id
            or not principal.active
            or principal.external_id != event.actor_wxid
        ):
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return None

        chatroom: Chatroom | None = None
        contact: Contact | None = None
        if source.chatroom_id is not None and source.contact_id is None:
            chatroom = await session.get(Chatroom, source.chatroom_id)
            if (
                event.conversation_type.value != "GROUP"
                or principal.principal_type is not PrincipalType.GROUP_MEMBER
                or chatroom is None
                or chatroom.bot_account_id != account.id
                or chatroom.chatroom_id != source.target_wxid
            ):
                self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
                return None
        elif source.contact_id is not None and source.chatroom_id is None:
            contact = await session.get(Contact, source.contact_id)
            if (
                event.conversation_type.value != "PRIVATE"
                or principal.principal_type is not PrincipalType.CONTACT
                or contact is None
                or contact.bot_account_id != account.id
                or contact.external_id != source.target_wxid
                or not contact.active
            ):
                self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
                return None
        else:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return None

        if not _scope_allows(
            context.revision_scope,
            workspace_id=context.workspace_id,
            account_id=account.id,
            chatroom_id=chatroom.id if chatroom is not None else None,
            contact_id=contact.id if contact is not None else None,
            conversation_id=source.target_wxid,
        ):
            self._reject(row, "MAIBOT_CONTEXT_SCOPE_MISMATCH")
            return None
        return _ResolvedConversationContext(
            source=source,
            account=account,
            chatroom=chatroom,
            contact=contact,
            authorization=authorization,
        )

    async def _accept_reply(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        row: MaiBotBridgeEnvelope,
        intent: MaiBotOutboundText,
        connector_context_id: str,
    ) -> None:
        resolved = await self._resolve_conversation_context(
            session,
            context=context,
            row=row,
            connector_context_id=connector_context_id,
        )
        if resolved is None:
            return
        source = resolved.source
        row.source_envelope_id = source.id
        row.trace_id = source.trace_id
        now = utc_now()
        if source.chatroom_id is not None and source.contact_id is None:
            source_target_kind = "GROUP"
        elif source.contact_id is not None and source.chatroom_id is None:
            source_target_kind = "PRIVATE"
        else:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return
        if (
            source.business_message_id != intent.reply_to_business_message_id
            or source.target_wxid != intent.target_wxid
            or source_target_kind != intent.target_kind
        ):
            self._reject(row, "MAIBOT_REPLY_TARGET_MISMATCH")
            return
        if TEXT_REPLY_ACTION_TYPE not in context.revision_grants:
            self._reject(row, "MAIBOT_REPLY_GRANT_MISSING")
            return
        if not await self._authorization_allowed(session, source):
            self._reject(row, "MAIBOT_REPLY_POLICY_DENIED")
            return
        authorization = resolved.authorization
        try:
            await self._outbox.enqueue_text(
                session,
                bot_account_id=cast(UUID, source.bot_account_id),
                trace_id=source.trace_id,
                idempotency_key=(f"maibot:{context.deployment_id}:reply:{intent.envelope_id}"),
                target_wxid=source.target_wxid,
                text=intent.text,
                expires_at=_as_utc(source.expires_at),
                action_type=TEXT_REPLY_ACTION_TYPE,
                authorization_context=authorization,
            )
        except (OutboxIdempotencyConflictError, ValueError):
            self._reject(row, "MAIBOT_REPLY_OUTBOX_REJECTED")
            return
        row.bot_account_id = source.bot_account_id
        row.actor_principal_id = source.actor_principal_id
        row.chatroom_id = source.chatroom_id
        row.contact_id = source.contact_id
        row.authorization_context = authorization.model_dump(mode="json")
        row.expires_at = source.expires_at
        row.status = MaiBotBridgeStatus.ACCEPTED
        row.completed_at = now
        row.last_error_code = "MAIBOT_QUOTE_DOWNGRADED_TO_TEXT"

    async def _accept_proactive(
        self,
        session: AsyncSession,
        *,
        context: MaiBotActivationContext,
        config: MaiBotConnectorConfig,
        row: MaiBotBridgeEnvelope,
        intent: MaiBotOutboundText,
        connector_context_id: str,
    ) -> None:
        if not config.enable_proactive_messages:
            self._reject(row, "MAIBOT_PROACTIVE_DISABLED")
            return
        if (
            MAIBOT_PROACTIVE_CAPABILITY not in context.revision_grants
            or TEXT_ACTION_TYPE not in context.revision_grants
        ):
            self._reject(row, "MAIBOT_PROACTIVE_GRANT_MISSING")
            return
        resolved = await self._resolve_conversation_context(
            session,
            context=context,
            row=row,
            connector_context_id=connector_context_id,
        )
        if resolved is None:
            return
        source = resolved.source
        if source.chatroom_id is not None and source.contact_id is None:
            source_target_kind = "GROUP"
        elif source.contact_id is not None and source.chatroom_id is None:
            source_target_kind = "PRIVATE"
        else:
            self._reject(row, "MAIBOT_SOURCE_CONTEXT_INVALID")
            return
        if source.target_wxid != intent.target_wxid or source_target_kind != intent.target_kind:
            self._reject(row, "MAIBOT_PROACTIVE_TARGET_MISMATCH")
            return
        account = resolved.account
        chatroom = resolved.chatroom
        contact = resolved.contact
        principal = await self._policy.create_principal(
            session,
            PrincipalCreate(
                workspace_id=context.workspace_id,
                principal_type=PrincipalType.CONNECTOR,
                external_id=f"maibot:{context.deployment_id}",
                display_name="MaiBot Connector",
            ),
        )
        authorization = OutboxAuthorizationContext(
            workspace_id=context.workspace_id,
            deployment_id=context.deployment_id,
            deployment_revision_id=context.deployment_revision_id,
            actor_principal_id=principal.id,
            chatroom_id=chatroom.id if chatroom is not None else None,
            contact_id=contact.id if contact is not None else None,
            resource_type=AclResourceType.CAPABILITY,
            resource_id=MAIBOT_PROACTIVE_CAPABILITY,
        )
        try:
            decision = await self._policy.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.workspace_id,
                    bot_account_id=account.id,
                    actor_principal_id=principal.id,
                    chatroom_id=authorization.chatroom_id,
                    contact_id=authorization.contact_id,
                    resource_type=authorization.resource_type,
                    resource_id=authorization.resource_id,
                    trace_id=row.trace_id,
                ),
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError):
            self._reject(row, "MAIBOT_PROACTIVE_POLICY_INVALID")
            return
        if not decision.allowed:
            self._reject(row, "MAIBOT_PROACTIVE_POLICY_DENIED")
            return
        try:
            await self._outbox.enqueue_text(
                session,
                bot_account_id=account.id,
                trace_id=row.trace_id,
                idempotency_key=(f"maibot:{context.deployment_id}:proactive:{intent.envelope_id}"),
                target_wxid=source.target_wxid,
                text=intent.text,
                expires_at=_as_utc(source.expires_at),
                action_type=TEXT_ACTION_TYPE,
                authorization_context=authorization,
            )
        except (OutboxIdempotencyConflictError, ValueError):
            self._reject(row, "MAIBOT_PROACTIVE_OUTBOX_REJECTED")
            return
        row.source_envelope_id = source.id
        row.bot_account_id = account.id
        row.actor_principal_id = principal.id
        row.chatroom_id = authorization.chatroom_id
        row.contact_id = authorization.contact_id
        row.target_wxid = source.target_wxid
        row.authorization_context = authorization.model_dump(mode="json")
        row.expires_at = source.expires_at
        row.status = MaiBotBridgeStatus.ACCEPTED
        row.completed_at = utc_now()
        row.last_error_code = None

    async def _authorization_allowed(
        self,
        session: AsyncSession,
        envelope: MaiBotBridgeEnvelope,
    ) -> bool:
        if envelope.bot_account_id is None:
            return False
        try:
            context = self._authorization_context(envelope)
            decision = await self._policy.evaluate(
                session,
                AclEvaluationRequest(
                    workspace_id=context.workspace_id,
                    bot_account_id=envelope.bot_account_id,
                    actor_principal_id=context.actor_principal_id,
                    chatroom_id=context.chatroom_id,
                    contact_id=context.contact_id,
                    resource_type=context.resource_type,
                    resource_id=context.resource_id,
                    parent_plugin_id=context.parent_plugin_id,
                    trace_id=envelope.trace_id,
                ),
            )
        except (InvalidPolicyRuleError, PolicyObjectNotFoundError, ValidationError):
            return False
        return decision.allowed

    @staticmethod
    def _authorization_context(
        envelope: MaiBotBridgeEnvelope,
    ) -> OutboxAuthorizationContext:
        return OutboxAuthorizationContext.model_validate(envelope.authorization_context)

    def _revision_config(
        self,
        revision: PluginDeploymentRevision,
    ) -> MaiBotConnectorConfig:
        try:
            raw = json.loads(self._cipher.decrypt(revision.config_ciphertext))
            return MaiBotConnectorConfig.model_validate(raw)
        except (CredentialDecryptionError, json.JSONDecodeError, ValidationError) as exc:
            raise MaiBotBridgeError("MaiBot connector configuration is unavailable") from exc

    def _issue_conversation_context(self, source_envelope_id: UUID) -> str:
        claims = MaiBotConversationContextClaims(
            source_envelope_id=source_envelope_id
        ).model_dump_json()
        token = self._cipher.encrypt(claims).decode("ascii")
        if len(token) > 255:
            raise MaiBotBridgeError("MaiBot conversation context exceeds protocol limit")
        return token

    @staticmethod
    async def _active_activation(
        session: AsyncSession,
        *,
        deployment_id: UUID,
        revision_id: UUID,
    ) -> PluginRevisionActivation | None:
        return cast(
            PluginRevisionActivation | None,
            await session.scalar(
                select(PluginRevisionActivation)
                .where(
                    PluginRevisionActivation.deployment_id == deployment_id,
                    PluginRevisionActivation.revision_id == revision_id,
                    PluginRevisionActivation.status == PluginActivationStatus.ACTIVE,
                )
                .order_by(PluginRevisionActivation.activation_epoch.desc())
            ),
        )

    @staticmethod
    def _reject(row: MaiBotBridgeEnvelope, error_code: str) -> None:
        row.status = MaiBotBridgeStatus.REJECTED
        row.completed_at = utc_now()
        row.last_error_code = error_code[:100]


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


def _connector_context_id(envelope: Mapping[str, Any]) -> str | None:
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        return None
    message_info = payload.get("message_info")
    if not isinstance(message_info, Mapping):
        return None
    additional_config = message_info.get("additional_config")
    if not isinstance(additional_config, Mapping):
        return None
    raw_context = additional_config.get("wechat_bot_connector_context_id")
    if not isinstance(raw_context, str):
        return None
    context_id = raw_context.strip()
    if not context_id or len(context_id) > 255:
        return None
    return context_id


def _sanitize_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    try:
        sanitized = json.loads(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise MaiBotProtocolError("MaiBot envelope is not valid JSON") from exc
    if not isinstance(sanitized, dict):
        raise MaiBotProtocolError("MaiBot envelope must be an object")
    meta = sanitized.get("meta")
    if isinstance(meta, dict) and "sender_user" in meta:
        meta["sender_user"] = MAIBOT_API_KEY_PLACEHOLDER
    payload = sanitized.get("payload")
    if isinstance(payload, dict):
        message_dim = payload.get("message_dim")
        if isinstance(message_dim, dict) and "api_key" in message_dim:
            message_dim["api_key"] = MAIBOT_API_KEY_PLACEHOLDER
    return sanitized


def _json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
