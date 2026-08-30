import { hasPermission } from "@/auth/permissions";
import { authSession } from "@/auth/session";

import { ApiError, apiRequest, createIdempotencyKey, toQuery } from "./client";
import type {
  AclRule,
  BotAccount,
  Contact,
  CreateConnectionInput,
  DiscoveredGroup,
  GeweConnection,
  ListResult,
  OverviewData,
  PermissionMatrix,
  PermissionResource,
  PermissionRule,
  PluginCatalog,
  PluginDeploymentView,
  PluginPackageView,
  PluginSummary,
  PluginView,
} from "./types";

interface RawList<T> {
  items?: T[];
  results?: T[];
  next_cursor?: string | null;
  total?: number | null;
}

export interface OverviewVisibility {
  connections: boolean;
  accounts: boolean;
  plugins: boolean;
  rules: boolean;
}

export type PermissionAwareOverview = OverviewData & {
  visibility: OverviewVisibility;
};

function requirePermission(permission: string, message: string): void {
  if (!hasPermission(authSession.state.user, permission)) {
    throw new ApiError(message, 403, "FRONTEND_PERMISSION_REQUIRED");
  }
}

function normalizeList<T>(payload: RawList<T> | T[]): ListResult<T> {
  if (Array.isArray(payload)) {
    return { items: payload, next_cursor: null, total: payload.length };
  }
  const items = payload.items || payload.results || [];
  return {
    items,
    next_cursor: payload.next_cursor ?? null,
    total: payload.total ?? items.length,
  };
}

function matchesSearch(search: string, values: Array<string | null | undefined>): boolean {
  const keyword = search.trim().toLocaleLowerCase();
  if (!keyword) return true;
  return values.some((value) => value?.toLocaleLowerCase().includes(keyword));
}

function filteredResult<T>(
  result: ListResult<T>,
  search: string,
  values: (item: T) => Array<string | null | undefined>,
): ListResult<T> {
  if (!search.trim()) return result;
  const items = result.items.filter((item) => matchesSearch(search, values(item)));
  return { items, next_cursor: null, total: items.length };
}

async function fetchAllPages<T>(
  path: string,
  params: Record<string, string | number | boolean | null | undefined> = {},
): Promise<ListResult<T>> {
  const limit = 200;
  let offset = 0;
  let total: number | null = null;
  const items: T[] = [];

  while (true) {
    const page = normalizeList(
      await apiRequest<RawList<T> | T[]>(`${path}${toQuery({ ...params, limit, offset })}`),
    );
    items.push(...page.items);
    total = page.total ?? total;
    if (!page.items.length || page.items.length < limit || (total !== null && items.length >= total)) break;
    offset += page.items.length;
  }

  return { items, next_cursor: null, total: total ?? items.length };
}

async function listConnections(search = ""): Promise<ListResult<GeweConnection>> {
  const result = normalizeList(await apiRequest<RawList<GeweConnection> | GeweConnection[]>("/connections"));
  return filteredResult(result, search, (item) => [item.name, item.api_base_url, item.status]);
}

async function listAccounts(search = ""): Promise<ListResult<BotAccount>> {
  requirePermission("account.read", "当前账号缺少 account.read 权限，无法读取微信账号");
  const result = await fetchAllPages<BotAccount>("/bot-accounts");
  return filteredResult(result, search, (item) => [
    item.nickname,
    item.wxid,
    item.alias,
    item.app_id,
    item.note,
  ]);
}

async function listContacts(botAccountId: string, search = ""): Promise<ListResult<Contact>> {
  requirePermission("directory.read", "当前账号缺少 directory.read 权限，无法读取通讯录");
  const result = await fetchAllPages<Contact>(
    `/directory/bot-accounts/${encodeURIComponent(botAccountId)}/contacts`,
  );
  return filteredResult(result, search, (item) => [
    item.nickname,
    item.remark,
    item.external_id,
    item.contact_type,
  ]);
}

async function listGroups(botAccountId: string, search = ""): Promise<ListResult<DiscoveredGroup>> {
  requirePermission("directory.read", "当前账号缺少 directory.read 权限，无法读取群目录");
  const result = await fetchAllPages<DiscoveredGroup>(
    `/directory/bot-accounts/${encodeURIComponent(botAccountId)}/chatrooms`,
  );
  return filteredResult(result, search, (item) => [
    item.name,
    item.chatroom_id,
    item.owner_wxid,
    item.discovered_from,
  ]);
}

function newestPackage(packages: PluginPackageView[]): PluginPackageView | undefined {
  return [...packages].sort((left, right) => right.created_at.localeCompare(left.created_at))[0];
}

function deploymentStatus(deployments: PluginDeploymentView[]): string | null {
  const priority = ["RUNNING", "STARTING", "DRAINING", "FAILED", "QUARANTINED", "STOPPED", "DRAFT"];
  return priority.find((status) => deployments.some((item) => item.status === status)) || deployments[0]?.status || null;
}

function pluginSource(plugin: PluginView, packageVersion?: PluginPackageView): string {
  const source = packageVersion?.manifest.source;
  if (typeof source === "string" && source.trim()) return source;
  return plugin.plugin_id.startsWith("builtin.") ? "内置" : "私有";
}

async function getPluginCatalog(): Promise<PluginCatalog> {
  return apiRequest<PluginCatalog>("/plugins");
}

async function listPlugins(search = ""): Promise<ListResult<PluginSummary>> {
  const catalog = await getPluginCatalog();
  const items = catalog.plugins.map((plugin) => {
    const packages = catalog.packages.filter((item) => item.plugin_id === plugin.id);
    const deployments = catalog.deployments.filter((item) => item.plugin_id === plugin.id);
    const latestPackage = newestPackage(packages);
    return {
      id: plugin.id,
      resource_id: plugin.plugin_id,
      name: plugin.name,
      description: plugin.description,
      latest_version: latestPackage?.semantic_version || null,
      package_status: latestPackage?.status || null,
      deployment_status: plugin.retired_at ? "RETIRED" : deploymentStatus(deployments),
      deployment_count: deployments.length,
      source: pluginSource(plugin, latestPackage),
      retired_at: plugin.retired_at,
    } satisfies PluginSummary;
  });
  const result = { items, next_cursor: null, total: items.length };
  return filteredResult(result, search, (item) => [item.name, item.resource_id, item.source]);
}

async function listRules(): Promise<ListResult<AclRule>> {
  return normalizeList(await apiRequest<RawList<AclRule> | AclRule[]>("/policy/rules"));
}

function isActiveRule(rule: AclRule, now: number): boolean {
  if (rule.revoked_at) return false;
  const validFrom = Date.parse(rule.valid_from);
  const validUntil = rule.valid_until ? Date.parse(rule.valid_until) : null;
  return (Number.isNaN(validFrom) || validFrom <= now) &&
    (validUntil === null || Number.isNaN(validUntil) || validUntil > now);
}

async function buildPermissionMatrix(): Promise<PermissionMatrix> {
  const [accounts, plugins, rules] = await Promise.all([listAccounts(), listPlugins(), listRules()]);
  const groupResults = await Promise.all(accounts.items.map((account) => listGroups(account.id)));
  const accountById = new Map(accounts.items.map((account) => [account.id, account]));
  const groups = groupResults.flatMap((result) => result.items).map((group) => {
    const account = accountById.get(group.bot_account_id);
    return {
      id: group.id,
      name: group.name || group.chatroom_id,
      account_name: account?.nickname || account?.wxid || account?.app_id || "未知账号",
      member_count: group.member_count,
    };
  });

  const now = Date.now();
  const groupRules = rules.items.filter(
    (rule) =>
      rule.scope_type === "CHATROOM" &&
      rule.principal_id === null &&
      rule.resource_type !== "CATEGORY" &&
      isActiveRule(rule, now),
  );
  const resources = new Map<string, PermissionResource>();
  for (const plugin of plugins.items) {
    resources.set(plugin.resource_id, {
      id: plugin.resource_id,
      name: plugin.name,
      type: "PLUGIN",
      status: plugin.deployment_status || plugin.package_status,
    });
  }
  for (const rule of groupRules) {
    if (!resources.has(rule.resource_id)) {
      resources.set(rule.resource_id, {
        id: rule.resource_id,
        name: rule.resource_id,
        type: rule.resource_type,
        status: null,
      });
    }
  }

  const matrixRules: PermissionRule[] = groupRules.map((rule) => ({
    group_id: rule.scope_id,
    resource_id: rule.resource_id,
    effect: rule.effect,
    locked: rule.locked,
    source: rule.reason,
    expires_at: rule.valid_until,
  }));

  return { groups, resources: [...resources.values()], rules: matrixRules };
}

async function getOverview(): Promise<PermissionAwareOverview> {
  const user = authSession.state.user;
  const visibility: OverviewVisibility = {
    connections: hasPermission(user, "connection.read"),
    accounts: hasPermission(user, "account.read"),
    plugins: hasPermission(user, "plugin.read"),
    rules: hasPermission(user, "policy.read"),
  };
  const [connections, accounts, plugins, rules] = await Promise.all([
    visibility.connections ? listConnections() : Promise.resolve(null),
    visibility.accounts ? listAccounts() : Promise.resolve(null),
    visibility.plugins ? listPlugins() : Promise.resolve(null),
    visibility.rules ? listRules() : Promise.resolve(null),
  ]);
  return {
    active_connections: connections?.items.filter((item) => item.status === "ACTIVE").length ?? 0,
    total_connections: connections ? (connections.total ?? connections.items.length) : 0,
    online_accounts: accounts?.items.filter((item) => item.status === "ONLINE").length ?? 0,
    total_accounts: accounts ? (accounts.total ?? accounts.items.length) : 0,
    plugin_count: plugins ? (plugins.total ?? plugins.items.length) : 0,
    rule_count: rules ? (rules.total ?? rules.items.length) : 0,
    messages_today: null,
    pending_approvals: null,
    agent_cost_today: null,
    updated_at: new Date().toISOString(),
    visibility,
  };
}

export const managementApi = {
  overview: getOverview,
  connections: {
    list: listConnections,
    create: (input: CreateConnectionInput) =>
      apiRequest<GeweConnection>("/connections", {
        method: "POST",
        headers: { "Idempotency-Key": createIdempotencyKey() },
        body: JSON.stringify(input),
      }),
  },
  accounts: {
    list: listAccounts,
  },
  contacts: {
    list: listContacts,
  },
  groups: {
    list: listGroups,
  },
  plugins: {
    list: listPlugins,
  },
  permissions: {
    matrix: buildPermissionMatrix,
  },
};
