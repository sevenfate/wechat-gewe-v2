export interface ListResult<T> {
  items: T[];
  next_cursor: string | null;
  total: number | null;
}

export interface AuthUser {
  id: string;
  username: string;
  display_name: string | null;
  roles: string[];
  permissions: string[];
}

export interface LoginInput {
  username: string;
  password: string;
}

export interface LoginSession {
  user: AuthUser;
  csrf_token: string;
  idle_expires_at: string;
  absolute_expires_at: string;
}

export interface BootstrapInput {
  username: string;
  display_name?: string;
  password: string;
}

export interface OverviewData {
  active_connections: number;
  total_connections: number;
  online_accounts: number;
  total_accounts: number;
  plugin_count: number;
  rule_count: number;
  messages_today: null;
  pending_approvals: null;
  agent_cost_today: null;
  updated_at: string;
}

export interface GeweConnection {
  id: string;
  workspace_id: string;
  name: string;
  api_base_url: string;
  token_fingerprint: string;
  callback_mode: "MANUAL" | "PLATFORM_MANAGED" | string;
  callback_url: string;
  callback_expected_url: string | null;
  callback_verified_at: string | null;
  last_callback_at: string | null;
  last_callback_error: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface CreateConnectionInput {
  workspace_slug?: string;
  workspace_name?: string;
  name: string;
  api_base_url: string;
  token: string;
}

export interface BotAccount {
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

export interface Contact {
  id: string;
  bot_account_id: string;
  external_id: string;
  contact_type: string;
  nickname: string | null;
  remark: string | null;
  avatar_url: string | null;
  active: boolean;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DiscoveredGroup {
  id: string;
  bot_account_id: string;
  chatroom_id: string;
  name: string | null;
  owner_wxid: string | null;
  member_count: number | null;
  discovered_from: string;
  placeholder: boolean;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PluginView {
  id: string;
  workspace_id: string;
  plugin_id: string;
  name: string;
  description: string;
  retired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PluginPackageView {
  id: string;
  plugin_id: string;
  semantic_version: string;
  package_sha256: string;
  manifest: Record<string, unknown>;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface PluginDeploymentView {
  id: string;
  workspace_id: string;
  plugin_id: string;
  name: string;
  status: string;
  active_revision_id: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface PluginCatalog {
  plugins: PluginView[];
  packages: PluginPackageView[];
  deployments: PluginDeploymentView[];
}

export interface PluginSummary {
  id: string;
  resource_id: string;
  name: string;
  description: string;
  latest_version: string | null;
  package_status: string | null;
  deployment_status: string | null;
  deployment_count: number;
  source: string;
  retired_at: string | null;
}

export type AclEffect = "ALLOW" | "ASK" | "DENY";

export interface AclRule {
  id: string;
  workspace_id: string;
  principal_id: string | null;
  scope_type: "WORKSPACE" | "BOT_ACCOUNT" | "CHATROOM" | "CONTACT" | string;
  scope_id: string;
  resource_type: "CATEGORY" | "PLUGIN" | "COMMAND" | "TOOL" | "AGENT" | "CAPABILITY" | string;
  resource_id: string;
  effect: AclEffect;
  locked: boolean;
  membership_epoch: number | null;
  valid_from: string;
  valid_until: string | null;
  reason: string;
  created_by: string;
  revoked_at: string | null;
  revoked_by: string | null;
  created_at: string;
  updated_at: string;
}

export type PermissionEffect = "INHERIT" | AclEffect;

export interface PermissionGroup {
  id: string;
  name: string;
  account_name: string;
  member_count: number | null;
}

export interface PermissionResource {
  id: string;
  name: string;
  type: string;
  status: string | null;
}

export interface PermissionRule {
  group_id: string;
  resource_id: string;
  effect: AclEffect;
  locked: boolean;
  source: string;
  expires_at: string | null;
}

export interface PermissionMatrix {
  groups: PermissionGroup[];
  resources: PermissionResource[];
  rules: PermissionRule[];
}
