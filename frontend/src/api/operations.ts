import { apiRequest, toQuery } from "./client";

export type OutboxStatus =
  | "PENDING"
  | "CLAIMED"
  | "SENDING"
  | "SENT"
  | "FAILED_RETRYABLE"
  | "FAILED_FINAL"
  | "UNKNOWN"
  | "CANCELLED";

export interface OutboxMessage {
  id: string;
  bot_account_id: string;
  trace_id: string;
  idempotency_key: string;
  action_type: string;
  target_wxid: string;
  payload: Record<string, unknown>;
  payload_sha256: string;
  status: OutboxStatus;
  priority: number;
  available_at: string;
  expires_at: string | null;
  attempt_count: number;
  last_error_code: string | null;
  last_attempt_started_at: string | null;
  last_attempt_finished_at: string | null;
  provider_message_id: string | null;
  provider_new_message_id: string | null;
  provider_create_time: number | null;
  provider_message_type: number | null;
  created_at: string;
  updated_at: string;
}

export interface AdminUser {
  id: string;
  username: string;
  display_name: string | null;
  status: "ACTIVE" | "DISABLED";
  auth_version: number;
  roles: string[];
  created_at: string;
  updated_at: string;
}

export interface RbacRole {
  id: string;
  code: string;
  name: string;
  is_system: boolean;
  active: boolean;
  permissions: string[];
  created_at: string;
  updated_at: string;
}

export interface RbacPermission {
  id: string;
  code: string;
  description: string | null;
}

interface ListResponse<T> {
  items: T[];
  total: number;
}

export const outboxApi = {
  list: (filters: {
    botAccountId?: string;
    status?: OutboxStatus | "";
    limit?: number;
    offset?: number;
  } = {}) =>
    apiRequest<ListResponse<OutboxMessage>>(
      `/outbox${toQuery({
        bot_account_id: filters.botAccountId,
        status: filters.status,
        limit: filters.limit ?? 200,
        offset: filters.offset ?? 0,
      })}`,
    ),
  cancel: (messageId: string, reason: string) =>
    apiRequest<OutboxMessage>(`/outbox/${encodeURIComponent(messageId)}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  reconcile: (messageId: string, resolution: "SENT" | "FAILED_FINAL", reason: string) =>
    apiRequest<OutboxMessage>(`/outbox/${encodeURIComponent(messageId)}/reconcile`, {
      method: "POST",
      body: JSON.stringify({ resolution, reason }),
    }),
};

export const adminRbacApi = {
  users: {
    list: () => apiRequest<ListResponse<AdminUser>>("/admin/users"),
    create: (input: { username: string; display_name?: string; password: string }) =>
      apiRequest<AdminUser>("/admin/users", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    setStatus: (userId: string, status: AdminUser["status"]) =>
      apiRequest<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/status`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    setRoles: (userId: string, roleCodes: string[]) =>
      apiRequest<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/roles`, {
        method: "PUT",
        body: JSON.stringify({ role_codes: roleCodes }),
      }),
  },
  roles: {
    list: () => apiRequest<ListResponse<RbacRole>>("/admin/roles"),
    create: (input: { code: string; name: string }) =>
      apiRequest<RbacRole>("/admin/roles", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    setPermissions: (roleId: string, permissionCodes: string[]) =>
      apiRequest<RbacRole>(`/admin/roles/${encodeURIComponent(roleId)}/permissions`, {
        method: "PUT",
        body: JSON.stringify({ permission_codes: permissionCodes }),
      }),
  },
  permissions: {
    list: () => apiRequest<ListResponse<RbacPermission>>("/admin/permissions"),
  },
};
