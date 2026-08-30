from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from wechat_bot.db.models import ConversationType, InboxStatus, OutboxStatus
from wechat_bot.db.policy_models import AclEffect


class MessageSummaryView(BaseModel):
    id: UUID
    inbox_id: UUID
    trace_id: UUID
    bot_account_id: UUID | None
    inbox_status: InboxStatus
    event_type: str
    conversation_type: ConversationType
    conversation_id: str | None
    actor_wxid: str | None
    provider_message_id: str | None
    text_preview: str
    occurred_at: datetime | None
    received_at: datetime
    error_code: str | None


class MessageList(BaseModel):
    items: list[MessageSummaryView]
    total: int


class MessageDetailView(MessageSummaryView):
    content: dict[str, Any]
    raw_payload: dict[str, Any]
    payload_sha256: str
    schema_version: str
    raw_ref: str


class TracePolicyDecisionView(BaseModel):
    id: UUID
    policy_version: int
    effect: AclEffect
    reason: str
    request_snapshot: dict[str, Any]
    matched_rule_ids: list[str]
    created_at: datetime


class TraceAuditEventView(BaseModel):
    id: UUID
    actor_type: str
    actor_id: str
    action: str
    object_type: str
    object_id: str
    result: str
    detail: dict[str, Any]
    created_at: datetime


class TraceOutboxView(BaseModel):
    id: UUID
    bot_account_id: UUID
    action_type: str
    target_wxid: str
    status: OutboxStatus
    attempt_count: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class TraceView(BaseModel):
    trace_id: UUID
    message: MessageDetailView | None
    policy_decisions: list[TracePolicyDecisionView]
    audit_events: list[TraceAuditEventView]
    outbox_messages: list[TraceOutboxView]
