import { apiRequest, toQuery } from "./client";

export type CallbackManagementMode = "MANUAL" | "PLATFORM_MANAGED";
export type ConnectionWritableStatus = "ACTIVE" | "DISABLED";
export type DeviceType = "ipad" | "mac";
export type LoginStatus = 0 | 1 | 2;

export interface WechatConnection {
  id: string;
  workspace_id: string;
  name: string;
  api_base_url: string;
  token_fingerprint: string;
  callback_mode: CallbackManagementMode;
  callback_url: string;
  callback_expected_url: string | null;
  callback_verified_at: string | null;
  last_callback_at: string | null;
  last_callback_error: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface WechatAccount {
  id: string;
  gewe_connection_id: string;
  app_id: string;
  wxid: string | null;
  alias: string | null;
  nickname: string | null;
  avatar_url: string | null;
  note: string | null;
  qr_expires_at: string | null;
  last_status_checked_at: string | null;
  last_status_error: string | null;
  status: string;
  logged_in_at: string | null;
  last_online_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ManualAccountInput {
  app_id: string;
  wxid?: string;
  note?: string;
}

export interface LoginQrCodeInput {
  device_type: DeviceType;
  region_id: string;
  app_id?: string;
  proxy_ip?: string;
  ttuid?: string;
  aid?: string;
}

export interface LoginQrCodeResult {
  account: WechatAccount;
  qr_data: string;
  qr_image_base64: string;
  uuid: string;
  expires_at: string;
}

export interface LoginCheckInput {
  auto_sliding?: boolean;
  proxy_ip?: string;
  captcha_code?: string;
}

export interface LoginCheckResult {
  account: WechatAccount;
  login_status: LoginStatus | null;
  verification_url: string | null;
}

export interface OnlineCheckResult {
  account: WechatAccount;
  online: boolean;
}

export interface ReconnectResult {
  account: WechatAccount;
  login_status: LoginStatus | null;
}

export interface CallbackApplyResult {
  connection: WechatConnection;
  applied: boolean;
}

export interface DirectorySyncResult {
  bot_account_id: string;
  observed_contacts: number;
  observed_chatrooms: number;
  synced_at: string;
}

export interface ChatroomMember {
  id: string;
  chatroom_id: string;
  member_wxid: string;
  membership_epoch: number;
  nickname: string | null;
  display_name: string | null;
  inviter_wxid: string | null;
  member_flag: number | null;
  joined_at: string;
  left_at: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ChatroomMemberList {
  items: ChatroomMember[];
  total: number;
}

export interface MembershipSyncResult {
  chatroom_id: string;
  observed_members: number;
  retained_unseen_active_members: number;
  snapshot_complete: boolean;
  synced_at: string;
}

export interface MembershipDepartureInput {
  membershipEpoch: number;
  reason: string;
}

interface ListResponse<T> {
  items: T[];
  total: number;
}

function resourceId(value: string): string {
  return encodeURIComponent(value);
}

function optionalText(value: string): string | undefined {
  const normalized = value.trim();
  return normalized || undefined;
}

async function listAllMembers(
  chatroomId: string,
  includeLeft = false,
): Promise<ChatroomMemberList> {
  const limit = 200;
  let offset = 0;
  let total = 0;
  const items: ChatroomMember[] = [];

  while (true) {
    const page = await apiRequest<ListResponse<ChatroomMember>>(
      `/directory/chatrooms/${resourceId(chatroomId)}/members${toQuery({
        include_left: includeLeft,
        limit,
        offset,
      })}`,
    );
    items.push(...page.items);
    total = page.total;
    if (!page.items.length || page.items.length < limit || items.length >= total) break;
    offset += page.items.length;
  }

  return { items, total };
}

export const wechatOperationsApi = {
  connections: {
    rotateToken: (connectionId: string, token: string) =>
      apiRequest<WechatConnection>(`/connections/${resourceId(connectionId)}/token`, {
        method: "PUT",
        body: JSON.stringify({ token }),
      }),
    setCallbackMode: (connectionId: string, callbackMode: CallbackManagementMode) =>
      apiRequest<WechatConnection>(`/connections/${resourceId(connectionId)}/callback-mode`, {
        method: "PUT",
        body: JSON.stringify({ callback_mode: callbackMode }),
      }),
    setStatus: (connectionId: string, status: ConnectionWritableStatus) =>
      apiRequest<WechatConnection>(`/connections/${resourceId(connectionId)}/status`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      }),
    applyManagedCallback: (connectionId: string) =>
      apiRequest<CallbackApplyResult>(
        `/connections/${resourceId(connectionId)}/callback/apply`,
        { method: "POST" },
      ),
  },
  accounts: {
    registerManual: (connectionId: string, input: ManualAccountInput) =>
      apiRequest<WechatAccount>(`/connections/${resourceId(connectionId)}/bot-accounts`, {
        method: "POST",
        body: JSON.stringify({
          app_id: input.app_id.trim(),
          wxid: optionalText(input.wxid || ""),
          note: optionalText(input.note || ""),
        }),
      }),
    getLoginQrCode: (connectionId: string, input: LoginQrCodeInput) =>
      apiRequest<LoginQrCodeResult>(`/connections/${resourceId(connectionId)}/login/qr-code`, {
        method: "POST",
        body: JSON.stringify({
          device_type: input.device_type,
          region_id: input.region_id.trim(),
          app_id: optionalText(input.app_id || "") || "",
          proxy_ip: optionalText(input.proxy_ip || ""),
          ttuid: optionalText(input.ttuid || ""),
          aid: optionalText(input.aid || ""),
        }),
      }),
    checkLogin: (accountId: string, input: LoginCheckInput = {}) =>
      apiRequest<LoginCheckResult>(`/bot-accounts/${resourceId(accountId)}/login/check`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    checkOnline: (accountId: string) =>
      apiRequest<OnlineCheckResult>(`/bot-accounts/${resourceId(accountId)}/check-online`, {
        method: "POST",
      }),
    reconnect: (accountId: string) =>
      apiRequest<ReconnectResult>(`/bot-accounts/${resourceId(accountId)}/reconnect`, {
        method: "POST",
      }),
    setDisabled: (accountId: string, disabled: boolean) =>
      apiRequest<WechatAccount>(`/bot-accounts/${resourceId(accountId)}/disabled`, {
        method: "PUT",
        body: JSON.stringify({ disabled }),
      }),
  },
  directory: {
    sync: (accountId: string) =>
      apiRequest<DirectorySyncResult>(
        `/directory/bot-accounts/${resourceId(accountId)}/sync`,
        { method: "POST" },
      ),
    listMembers: listAllMembers,
    syncMembers: (chatroomId: string) =>
      apiRequest<MembershipSyncResult>(
        `/directory/chatrooms/${resourceId(chatroomId)}/sync-members`,
        { method: "POST" },
      ),
    markMembershipLeft: (
      chatroomId: string,
      membershipId: string,
      input: MembershipDepartureInput,
    ) =>
      apiRequest<ChatroomMember>(
        `/directory/chatrooms/${resourceId(chatroomId)}/memberships/${resourceId(membershipId)}/mark-left`,
        {
          method: "POST",
          body: JSON.stringify({
            membership_epoch: input.membershipEpoch,
            reason: input.reason.trim(),
          }),
        },
      ),
  },
};
