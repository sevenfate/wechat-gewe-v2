import { apiRequest, toQuery } from "./client";

export type ToolCallStatus =
  | "RECEIVED"
  | "AUTHORIZED"
  | "EXECUTING"
  | "SUCCEEDED"
  | "FAILED_RETRYABLE"
  | "FAILED_FINAL"
  | "DENIED"
  | "CANCELLED"
  | "UNKNOWN";

export type ToolInvocationMode = "USER_REQUESTED" | "AUTONOMOUS";

export interface ToolCall {
  id: string;
  workspace_id: string;
  connector_deployment_id: string;
  connector_revision_id: string;
  connector_activation_id: string;
  target_deployment_id: string | null;
  target_revision_id: string | null;
  target_activation_epoch: number | null;
  external_tool_call_id: string;
  connector_context_digest: string;
  tool_name: string;
  tool_schema_version: string;
  invocation_mode: ToolInvocationMode;
  arguments: Record<string, unknown>;
  arguments_sha256: string;
  trace_id: string;
  actor_principal_id: string | null;
  bot_account_id: string | null;
  chatroom_id: string | null;
  contact_id: string | null;
  status: ToolCallStatus;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_detail: string | null;
  deadline_at: string;
  available_at: string;
  attempt_count: number;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ToolCallListResponse {
  items: ToolCall[];
  total: number;
}

export const toolBridgeApi = {
  list: (filters: {
    status?: ToolCallStatus | "";
    toolName?: string;
    limit?: number;
    offset?: number;
  } = {}) =>
    apiRequest<ToolCallListResponse>(
      `/tool-bridge/calls${toQuery({
        status: filters.status,
        tool_name: filters.toolName,
        limit: filters.limit ?? 100,
        offset: filters.offset ?? 0,
      })}`,
    ),
  get: (id: string) =>
    apiRequest<ToolCall>(`/tool-bridge/calls/${encodeURIComponent(id)}`),
};
