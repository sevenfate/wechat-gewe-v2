import { apiRequest, toQuery } from "./client";
import { managementApi } from "./resources";
import type { AclRule, PluginCatalog } from "./types";

export type EditablePolicyEffect = "INHERIT" | "ALLOW" | "DENY";

export interface PolicyGroup {
  id: string;
  workspace_id: string;
  bot_account_id: string;
  name: string;
  account_name: string;
  member_count: number | null;
}

export interface PolicyPlugin {
  id: string;
  workspace_id: string;
  resource_id: string;
  name: string;
  description: string;
  retired: boolean;
}

export interface PolicyDataset {
  groups: PolicyGroup[];
  plugins: PolicyPlugin[];
  rules: AclRule[];
}

export interface GroupMember {
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

export interface PolicyPrincipal {
  id: string;
  workspace_id: string;
  principal_type: string;
  external_id: string;
  display_name: string | null;
  active: boolean;
  created_at: string;
  updated_at: string;
}

interface GroupMemberPrincipalLookup {
  workspace_id: string;
  chatroom_id: string;
  membership_id: string;
  principal: PolicyPrincipal | null;
}

interface ListResponse<T> {
  items: T[];
  total: number;
}

interface RuleTarget {
  workspaceId: string;
  groupId: string;
  botAccountId: string;
  resourceId: string;
  principalId: string | null;
  membershipEpoch: number | null;
}

export interface ReplaceRuleInput extends RuleTarget {
  currentRules: AclRule[];
  effect: EditablePolicyEffect;
  reason: string;
}

export interface ReplaceRuleResult {
  rule: AclRule | null;
  revokedRuleIds: string[];
}

export class PolicyChangeError extends Error {
  readonly fallbackApplied: boolean;
  readonly causeMessage: string;

  constructor(message: string, fallbackApplied: boolean, causeMessage: string) {
    super(message);
    this.name = "PolicyChangeError";
    this.fallbackApplied = fallbackApplied;
    this.causeMessage = causeMessage;
  }
}

function memberLabel(member: GroupMember): string {
  return member.display_name || member.nickname || member.member_wxid;
}

async function loadDataset(): Promise<PolicyDataset> {
  const [connections, accounts, catalog, ruleList] = await Promise.all([
    managementApi.connections.list(),
    managementApi.accounts.list(),
    apiRequest<PluginCatalog>("/plugins"),
    apiRequest<ListResponse<AclRule>>("/policy/rules"),
  ]);
  const groupLists = await Promise.all(
    accounts.items.map((account) => managementApi.groups.list(account.id)),
  );
  const connectionById = new Map(connections.items.map((connection) => [connection.id, connection]));
  const accountById = new Map(accounts.items.map((account) => [account.id, account]));

  const groups = groupLists
    .flatMap((result) => result.items)
    .map((group) => {
      const account = accountById.get(group.bot_account_id);
      const connection = account ? connectionById.get(account.gewe_connection_id) : undefined;
      return {
        id: group.id,
        workspace_id: connection?.workspace_id || "",
        bot_account_id: group.bot_account_id,
        name: group.name || group.chatroom_id,
        account_name: account?.nickname || account?.wxid || account?.app_id || "未知账号",
        member_count: group.member_count,
      } satisfies PolicyGroup;
    })
    .sort((left, right) =>
      `${left.account_name}\0${left.name}`.localeCompare(`${right.account_name}\0${right.name}`, "zh-CN"),
    );

  const plugins: PolicyPlugin[] = catalog.plugins.map((plugin) => ({
    id: plugin.id,
    workspace_id: plugin.workspace_id,
    resource_id: plugin.plugin_id,
    name: plugin.name,
    description: plugin.description,
    retired: plugin.retired_at !== null,
  }));
  const pluginKeys = new Set(plugins.map((plugin) => `${plugin.workspace_id}\0${plugin.resource_id}`));
  for (const rule of ruleList.items) {
    const key = `${rule.workspace_id}\0${rule.resource_id}`;
    if (rule.resource_type === "PLUGIN" && !pluginKeys.has(key)) {
      plugins.push({
        id: key,
        workspace_id: rule.workspace_id,
        resource_id: rule.resource_id,
        name: rule.resource_id,
        description: "仅存在于 ACL 规则中的插件资源",
        retired: true,
      });
      pluginKeys.add(key);
    }
  }
  plugins.sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));

  return { groups, plugins, rules: ruleList.items };
}

async function listMembers(chatroomId: string): Promise<GroupMember[]> {
  const limit = 200;
  let offset = 0;
  let total = 0;
  const members: GroupMember[] = [];

  do {
    const page = await apiRequest<ListResponse<GroupMember>>(
      `/directory/chatrooms/${encodeURIComponent(chatroomId)}/members${toQuery({
        include_left: false,
        limit,
        offset,
      })}`,
    );
    members.push(...page.items);
    total = page.total;
    if (!page.items.length) break;
    offset += page.items.length;
  } while (members.length < total);

  return members
    .filter((member) => member.active)
    .sort((left, right) => memberLabel(left).localeCompare(memberLabel(right), "zh-CN"));
}

async function ensureGroupMemberPrincipal(
  workspaceId: string,
  chatroomId: string,
  member: GroupMember,
): Promise<PolicyPrincipal> {
  return apiRequest<PolicyPrincipal>("/policy/principals/group-member", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      chatroom_id: chatroomId,
      membership_id: member.id,
    }),
  });
}

async function lookupGroupMemberPrincipal(
  workspaceId: string,
  chatroomId: string,
  membershipId: string,
): Promise<PolicyPrincipal | null> {
  const result = await apiRequest<GroupMemberPrincipalLookup>(
    `/policy/principals/group-member${toQuery({
      workspace_id: workspaceId,
      chatroom_id: chatroomId,
      membership_id: membershipId,
    })}`,
  );
  return result.principal;
}

function exactRules(rules: AclRule[], target: RuleTarget): AclRule[] {
  return rules.filter(
    (rule) =>
      rule.revoked_at === null &&
      rule.workspace_id === target.workspaceId &&
      rule.principal_id === target.principalId &&
      rule.membership_epoch === target.membershipEpoch &&
      rule.scope_type === "CHATROOM" &&
      rule.scope_id === target.groupId &&
      rule.resource_type === "PLUGIN" &&
      rule.resource_id === target.resourceId,
  );
}

function isRuleActive(rule: AclRule, now = Date.now()): boolean {
  if (rule.revoked_at) return false;
  const validFrom = Date.parse(rule.valid_from);
  const validUntil = rule.valid_until ? Date.parse(rule.valid_until) : null;
  return (Number.isNaN(validFrom) || validFrom <= now) &&
    (validUntil === null || Number.isNaN(validUntil) || validUntil > now);
}

export function configuredEffect(rules: AclRule[], target: RuleTarget): EditablePolicyEffect {
  const active = exactRules(rules, target).filter((rule) => isRuleActive(rule));
  if (!active.length) return "INHERIT";
  if (active.some((rule) => rule.effect === "DENY" || rule.effect === "ASK")) return "DENY";
  return "ALLOW";
}

export function configurationMatches(
  rules: AclRule[],
  target: RuleTarget,
  effect: EditablePolicyEffect,
): boolean {
  const exact = exactRules(rules, target);
  if (effect === "INHERIT") return exact.length === 0;
  return exact.length === 1 && exact[0].effect === effect && isRuleActive(exact[0]);
}

export function blockingLockedRule(
  rules: AclRule[],
  target: RuleTarget,
): AclRule | undefined {
  const exactLocked = exactRules(rules, target).find(
    (rule) => rule.locked && rule.effect === "DENY" && isRuleActive(rule),
  );
  if (exactLocked) return exactLocked;

  return rules.find((rule) => {
    if (!rule.locked || rule.effect !== "DENY" || !isRuleActive(rule)) return false;
    if (rule.workspace_id !== target.workspaceId) return false;
    if (rule.principal_id !== null && rule.principal_id !== target.principalId) return false;
    if (
      rule.principal_id !== null &&
      (target.membershipEpoch === null || rule.membership_epoch !== target.membershipEpoch)
    ) {
      return false;
    }
    const resourceMatches =
      (rule.resource_type === "PLUGIN" && rule.resource_id === target.resourceId) ||
      (rule.resource_type === "CATEGORY" && rule.resource_id === "*");
    if (!resourceMatches) return false;
    return (
      (rule.scope_type === "WORKSPACE" && rule.scope_id === target.workspaceId) ||
      (rule.scope_type === "BOT_ACCOUNT" && rule.scope_id === target.botAccountId) ||
      (rule.scope_type === "CHATROOM" && rule.scope_id === target.groupId)
    );
  });
}

async function createPluginRule(
  target: RuleTarget,
  effect: "ALLOW" | "DENY",
  reason: string,
): Promise<AclRule> {
  return apiRequest<AclRule>("/policy/rules", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: target.workspaceId,
      principal_id: target.principalId,
      scope_type: "CHATROOM",
      scope_id: target.groupId,
      resource_type: "PLUGIN",
      resource_id: target.resourceId,
      effect,
      locked: false,
      membership_epoch: target.membershipEpoch,
      reason,
    }),
  });
}

async function replacePluginRule(input: ReplaceRuleInput): Promise<ReplaceRuleResult> {
  const target: RuleTarget = input;
  const current = exactRules(input.currentRules, target);
  const locked = blockingLockedRule(input.currentRules, target);
  if (locked) {
    throw new PolicyChangeError(`锁定规则不可覆盖：${locked.reason}`, false, locked.reason);
  }

  const revokedRuleIds: string[] = [];
  try {
    for (const rule of current) {
      await apiRequest<AclRule>(`/policy/rules/${encodeURIComponent(rule.id)}/revoke`, {
        method: "POST",
      });
      revokedRuleIds.push(rule.id);
    }
    if (input.effect === "INHERIT") return { rule: null, revokedRuleIds };
    const rule = await createPluginRule(target, input.effect, input.reason);
    return { rule, revokedRuleIds };
  } catch (caught) {
    const causeMessage = caught instanceof Error ? caught.message : "未知错误";
    try {
      await createPluginRule(target, "DENY", `安全回退：${input.reason}`);
      throw new PolicyChangeError(
        `权限更新失败，已自动写入拒绝规则：${causeMessage}`,
        true,
        causeMessage,
      );
    } catch (fallbackCaught) {
      if (fallbackCaught instanceof PolicyChangeError) throw fallbackCaught;
      const fallbackMessage = fallbackCaught instanceof Error ? fallbackCaught.message : "未知错误";
      throw new PolicyChangeError(
        `权限更新失败，且无法确认安全拒绝已写入：${causeMessage}；回退失败：${fallbackMessage}`,
        false,
        causeMessage,
      );
    }
  }
}

export const policyManagementApi = {
  loadDataset,
  listMembers,
  lookupGroupMemberPrincipal,
  ensureGroupMemberPrincipal,
  replacePluginRule,
};
