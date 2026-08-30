export interface ListResult<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}

export interface OverviewData {
  online_accounts?: number | null;
  total_accounts?: number | null;
  messages_today?: number | null;
  queue_depth?: number | null;
  failures_today?: number | null;
  pending_approvals?: number | null;
  active_alerts?: number | null;
  agent_cost_today?: {
    amount?: number | null;
    currency?: string | null;
  } | null;
  updated_at?: string | null;
}

export interface GeweConnection {
  id: string;
  name: string;
  base_url: string;
  token_masked?: string | null;
  callback_mode: "MANUAL" | "PLATFORM_MANAGED" | string;
  callback_url?: string | null;
  callback_verified_at?: string | null;
  health_status?: string | null;
  account_count?: number | null;
  updated_at?: string | null;
}

export interface CreateConnectionInput {
  name: string;
  base_url: string;
  token: string;
  callback_mode: "MANUAL" | "PLATFORM_MANAGED";
}

export interface BotAccount {
  id: string;
  connection_id?: string | null;
  nickname?: string | null;
  wxid: string;
  app_id: string;
  avatar_url?: string | null;
  status?: string | null;
  sync_status?: string | null;
  last_online_at?: string | null;
  remark?: string | null;
}

export interface Contact {
  id: string;
  bot_account_id?: string | null;
  external_id: string;
  nickname?: string | null;
  remark?: string | null;
  contact_type?: string | null;
  status?: string | null;
  last_synced_at?: string | null;
}

export interface DiscoveredGroup {
  id: string;
  bot_account_id?: string | null;
  chatroom_id: string;
  name?: string | null;
  owner_wxid?: string | null;
  avatar_url?: string | null;
  member_count?: number | null;
  discovery_source?: string | null;
  freshness?: string | null;
  updated_at?: string | null;
}

export interface PluginSummary {
  id: string;
  name: string;
  description?: string | null;
  latest_version?: string | null;
  status?: string | null;
  trust_status?: string | null;
  health_status?: string | null;
  deployment_count?: number | null;
  source?: string | null;
}

export type PermissionEffect = "INHERIT" | "ALLOW" | "DENY";

export interface PermissionGroup {
  id: string;
  name: string;
  account_name?: string | null;
  member_count?: number | null;
}

export interface PermissionResource {
  id: string;
  name: string;
  type: "PLUGIN" | "CONNECTOR" | "AGENT" | string;
  status?: string | null;
}

export interface PermissionRule {
  group_id: string;
  resource_id: string;
  effect: PermissionEffect;
  source?: string | null;
  expires_at?: string | null;
}

export interface PermissionMatrix {
  groups: PermissionGroup[];
  resources: PermissionResource[];
  rules: PermissionRule[];
  revision?: string | null;
}
