import { apiRequest, toQuery } from "./client";

export interface MessageSummary {
  id: string;
  inbox_id: string;
  trace_id: string;
  bot_account_id: string | null;
  inbox_status: string;
  event_type: string;
  conversation_type: string;
  conversation_id: string | null;
  actor_wxid: string | null;
  provider_message_id: string | null;
  text_preview: string;
  occurred_at: string | null;
  received_at: string;
  error_code: string | null;
}

export interface MessageDetail extends MessageSummary {
  content: Record<string, unknown>;
  raw_payload: Record<string, unknown>;
  payload_sha256: string;
  schema_version: string;
  raw_ref: string;
}

export interface TraceView {
  trace_id: string;
  message: MessageDetail | null;
  policy_decisions: Array<{
    id: string;
    policy_version: number;
    effect: string;
    reason: string;
    request_snapshot: Record<string, unknown>;
    matched_rule_ids: string[];
    created_at: string;
  }>;
  audit_events: Array<{
    id: string;
    actor_type: string;
    actor_id: string;
    action: string;
    object_type: string;
    object_id: string;
    result: string;
    detail: Record<string, unknown>;
    created_at: string;
  }>;
  outbox_messages: Array<{
    id: string;
    bot_account_id: string;
    action_type: string;
    target_wxid: string;
    status: string;
    attempt_count: number;
    last_error_code: string | null;
    created_at: string;
    updated_at: string;
  }>;
}

interface MessageList {
  items: MessageSummary[];
  total: number;
}

export const observabilityApi = {
  messages: (filters: {
    botAccountId?: string;
    status?: string;
    conversationType?: string;
    limit?: number;
    offset?: number;
  } = {}) =>
    apiRequest<MessageList>(
      `/messages${toQuery({
        bot_account_id: filters.botAccountId,
        status: filters.status,
        conversation_type: filters.conversationType,
        limit: filters.limit ?? 200,
        offset: filters.offset ?? 0,
      })}`,
    ),
  message: (eventId: string) =>
    apiRequest<MessageDetail>(`/messages/${encodeURIComponent(eventId)}`),
  trace: (traceId: string) =>
    apiRequest<TraceView>(`/traces/${encodeURIComponent(traceId)}`),
};
