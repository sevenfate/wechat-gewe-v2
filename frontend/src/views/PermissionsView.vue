<script setup lang="ts">
import { AlertTriangle, LockKeyhole, ShieldCheck, UserRound, UsersRound } from "lucide-vue-next";
import { computed, onMounted, ref, shallowRef, watch } from "vue";

import { ApiError } from "@/api/client";
import {
  blockingLockedRule,
  configurationMatches,
  configuredEffect,
  PolicyChangeError,
  policyManagementApi,
  type EditablePolicyEffect,
  type GroupMember,
  type PolicyDataset,
  type PolicyPlugin,
  type PolicyPrincipal,
} from "@/api/policy-management";
import { hasAllPermissions } from "@/auth/permissions";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import { formatInteger } from "@/utils/format";
import "@/styles/permissions.css";

const effects: Array<{ value: EditablePolicyEffect; label: string }> = [
  { value: "INHERIT", label: "继承" },
  { value: "ALLOW", label: "允许" },
  { value: "DENY", label: "拒绝" },
];

const data = shallowRef<PolicyDataset | null>(null);
const loading = ref(false);
const loadError = shallowRef<ApiError | null>(null);
const search = ref("");
const selectedGroupId = ref("");
const members = shallowRef<GroupMember[]>([]);
const membersLoading = ref(false);
const membersError = shallowRef<ApiError | null>(null);
const loadedMembersGroupId = ref("");
const selectedMemberId = ref("");
const memberPrincipal = shallowRef<PolicyPrincipal | null>(null);
const principalLoading = ref(false);
const principalResolved = ref(false);
const principalError = ref("");
const savingKey = ref("");
const actionError = ref("");
const actionNotice = ref("");
let memberRequestEpoch = 0;
let principalRequestEpoch = 0;
let datasetRequestEpoch = 0;

const DATASET_READ_PERMISSIONS = [
  "policy.read",
  "directory.read",
  "account.read",
  "connection.read",
  "plugin.read",
] as const;

const canReadDataset = computed(() =>
  hasAllPermissions(authSession.state.user, DATASET_READ_PERMISSIONS),
);

const canWrite = computed(() => {
  const user = authSession.state.user;
  return Boolean(user?.roles.includes("owner") || user?.permissions.includes("policy.write"));
});

const selectedGroup = computed(() =>
  data.value?.groups.find((group) => group.id === selectedGroupId.value),
);

const visiblePlugins = computed(() => {
  const group = selectedGroup.value;
  if (!group || !data.value) return [];
  const keyword = search.value.trim().toLocaleLowerCase();
  return data.value.plugins.filter(
    (plugin) =>
      plugin.workspace_id === group.workspace_id &&
      (!keyword ||
        [plugin.name, plugin.resource_id, plugin.description].some((value) =>
          value.toLocaleLowerCase().includes(keyword),
        )),
  );
});

const selectedMember = computed(() =>
  members.value.find((member) => member.id === selectedMemberId.value),
);

const selectedGroupMemberCount = computed(() => {
  const group = selectedGroup.value;
  if (!group) return "";
  if (loadedMembersGroupId.value === group.id) {
    return `${formatInteger(members.value.length)} 名成员`;
  }
  return group.member_count === null
    ? "成员数待加载"
    : `${formatInteger(group.member_count)} 名成员`;
});

function asApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError ? caught : new ApiError(fallback, 0, "UNKNOWN_ERROR");
}

function memberLabel(member: GroupMember): string {
  const name = member.display_name || member.nickname;
  return name ? `${name} (${member.member_wxid})` : member.member_wxid;
}

function targetFor(plugin: PolicyPlugin, principalId: string | null) {
  const group = selectedGroup.value;
  if (!group) return null;
  return {
    workspaceId: group.workspace_id,
    groupId: group.id,
    botAccountId: group.bot_account_id,
    resourceId: plugin.resource_id,
    principalId,
    membershipEpoch: principalId ? selectedMember.value?.membership_epoch ?? null : null,
  };
}

function groupEffect(plugin: PolicyPlugin): EditablePolicyEffect {
  const target = targetFor(plugin, null);
  return target && data.value ? configuredEffect(data.value.rules, target) : "DENY";
}

function memberEffect(plugin: PolicyPlugin): EditablePolicyEffect {
  if (!selectedMemberId.value || !principalResolved.value || !data.value) return "INHERIT";
  if (!memberPrincipal.value) return "INHERIT";
  const target = targetFor(plugin, memberPrincipal.value.id);
  if (!target) return "INHERIT";
  return configuredEffect(data.value.rules, target);
}

function lockedReason(plugin: PolicyPlugin, principalId: string | null): string {
  const target = targetFor(plugin, principalId);
  if (!target || !data.value) return "";
  const locked = blockingLockedRule(data.value.rules, target);
  return locked ? locked.reason : "";
}

function controlTitle(plugin: PolicyPlugin, principalId: string | null): string {
  if (!canWrite.value) return "当前账号缺少 policy.write 权限";
  if (!selectedGroup.value?.workspace_id) return "无法确定该群所属工作区";
  const reason = lockedReason(plugin, principalId);
  return reason ? `锁定拒绝不可覆盖：${reason}` : "";
}

function controlDisabled(plugin: PolicyPlugin, principalId: string | null): boolean {
  return (
    !canWrite.value ||
    loading.value ||
    !selectedGroup.value?.workspace_id ||
    Boolean(savingKey.value) ||
    Boolean(lockedReason(plugin, principalId))
  );
}

function memberControlDisabled(
  plugin: PolicyPlugin,
  effect: EditablePolicyEffect,
): boolean {
  if (!selectedMember.value || !principalResolved.value || principalError.value) return true;
  if (!memberPrincipal.value && effect === "INHERIT") return true;
  return controlDisabled(plugin, memberPrincipal.value?.id || null);
}

function memberControlTitle(plugin: PolicyPlugin): string {
  const baseTitle = controlTitle(plugin, memberPrincipal.value?.id || null);
  if (baseTitle) return baseTitle;
  return memberPrincipal.value ? "" : "该成员尚无独立规则，当前继承群级权限";
}

async function reload() {
  const requestEpoch = ++datasetRequestEpoch;
  const previousGroupId = selectedGroupId.value;
  const previousMemberId = selectedMemberId.value;
  memberRequestEpoch += 1;
  principalRequestEpoch += 1;
  membersLoading.value = false;
  principalLoading.value = false;
  memberPrincipal.value = null;
  principalResolved.value = false;
  principalError.value = "";
  loading.value = true;
  loadError.value = null;
  if (!canReadDataset.value) {
    const missing = DATASET_READ_PERMISSIONS.filter(
      (permission) => !hasAllPermissions(authSession.state.user, [permission]),
    );
    loadError.value = new ApiError(
      `权限矩阵还需要以下只读权限：${missing.join("、")}`,
      403,
      "FRONTEND_PERMISSION_REQUIRED",
    );
    loading.value = false;
    return;
  }
  try {
    const result = await policyManagementApi.loadDataset();
    if (requestEpoch !== datasetRequestEpoch) return;
    data.value = result;
    if (!result.groups.some((group) => group.id === selectedGroupId.value)) {
      selectedGroupId.value = result.groups[0]?.id || "";
    }
    if (
      requestEpoch === datasetRequestEpoch &&
      previousGroupId &&
      previousGroupId === selectedGroupId.value
    ) {
      await reloadMembers(previousMemberId);
    }
  } catch (caught) {
    if (requestEpoch === datasetRequestEpoch) {
      loadError.value = asApiError(caught, "加载权限数据时发生未知错误");
    }
  } finally {
    if (requestEpoch === datasetRequestEpoch) loading.value = false;
  }
}

async function reloadMembers(preferredMemberId = "") {
  const groupId = selectedGroupId.value;
  const epoch = ++memberRequestEpoch;
  principalRequestEpoch += 1;
  membersLoading.value = false;
  principalLoading.value = false;
  memberPrincipal.value = null;
  principalResolved.value = false;
  principalError.value = "";
  selectedMemberId.value = "";
  members.value = [];
  loadedMembersGroupId.value = "";
  membersError.value = null;
  if (!groupId) return;

  membersLoading.value = true;
  try {
    const result = await policyManagementApi.listMembers(groupId);
    if (epoch === memberRequestEpoch && selectedGroupId.value === groupId) {
      members.value = result;
      loadedMembersGroupId.value = groupId;
      if (result.some((member) => member.id === preferredMemberId)) {
        selectedMemberId.value = preferredMemberId;
      }
    }
  } catch (caught) {
    if (epoch === memberRequestEpoch) {
      membersError.value = asApiError(caught, "加载群成员时发生未知错误");
    }
  } finally {
    if (epoch === memberRequestEpoch) membersLoading.value = false;
  }
}

async function resolveMemberPrincipal() {
  const member = selectedMember.value;
  const group = selectedGroup.value;
  const epoch = ++principalRequestEpoch;
  memberPrincipal.value = null;
  principalResolved.value = false;
  principalError.value = "";
  principalLoading.value = false;
  if (!member || !group) return;

  principalLoading.value = true;
  try {
    const principal = await policyManagementApi.lookupGroupMemberPrincipal(
      group.workspace_id,
      group.id,
      member.id,
    );
    if (epoch === principalRequestEpoch && selectedMemberId.value === member.id) {
      memberPrincipal.value = principal;
      principalResolved.value = true;
    }
  } catch (caught) {
    if (epoch === principalRequestEpoch) {
      principalError.value = asApiError(caught, "无法解析群成员身份").message;
    }
  } finally {
    if (epoch === principalRequestEpoch) principalLoading.value = false;
  }
}

async function setEffect(
  plugin: PolicyPlugin,
  effect: EditablePolicyEffect,
  principal: PolicyPrincipal | null,
  memberMode = false,
) {
  const group = selectedGroup.value;
  const member = selectedMember.value;
  if (!group || !data.value || !canWrite.value) return;
  if (memberMode && (!member || !principalResolved.value || principalError.value)) return;
  if (controlDisabled(plugin, principal?.id || null)) return;
  if (memberMode && !principal && effect === "INHERIT") return;

  let effectivePrincipal = principal;
  let target = targetFor(plugin, effectivePrincipal?.id || null);
  if (!target) return;
  if ((!memberMode || effectivePrincipal) && configurationMatches(data.value.rules, target, effect)) {
    return;
  }

  savingKey.value = `${memberMode ? "member" : "group"}:${plugin.id}`;
  actionError.value = "";
  actionNotice.value = "";
  const subject = memberMode ? memberLabel(member!) : group.name;
  try {
    if (memberMode && !effectivePrincipal) {
      effectivePrincipal = await policyManagementApi.ensureGroupMemberPrincipal(
        group.workspace_id,
        group.id,
        member!,
      );
      memberPrincipal.value = effectivePrincipal;
      target = targetFor(plugin, effectivePrincipal.id);
      if (!target) return;
    }
    await policyManagementApi.replacePluginRule({
      ...target,
      currentRules: data.value.rules,
      effect,
      reason: `管理后台设置 ${subject} 对 ${plugin.name} 的权限`,
    });
    actionNotice.value = `${subject} / ${plugin.name} 已设为${effects.find((item) => item.value === effect)?.label}`;
  } catch (caught) {
    actionError.value =
      caught instanceof PolicyChangeError
        ? caught.message
        : asApiError(caught, "权限更新失败").message;
  } finally {
    await reload();
    savingKey.value = "";
  }
}

watch(selectedGroupId, () => {
  memberPrincipal.value = null;
  principalResolved.value = false;
  principalError.value = "";
  void reloadMembers();
});

watch(selectedMemberId, () => {
  void resolveMemberPrincipal();
});

onMounted(reload);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="权限矩阵" description="配置群级插件权限与群成员例外" />

    <section class="notice-band notice-band--neutral permission-policy-notice">
      <ShieldCheck :size="18" />
      <span>成员例外优先于普通群规则；任一适用的 locked deny 均不可覆盖。</span>
    </section>

    <section v-if="!canWrite" class="permission-readonly" role="status">
      <LockKeyhole :size="17" />
      <span>当前账号可选择群成员审计现有例外；编辑需要 <code>policy.write</code>。</span>
    </section>

    <div v-if="actionError" class="permission-action-message permission-action-message--error" role="alert">
      <AlertTriangle :size="17" />
      <span>{{ actionError }}</span>
    </div>
    <div v-else-if="actionNotice" class="permission-action-message permission-action-message--success" role="status">
      <ShieldCheck :size="17" />
      <span>{{ actionNotice }}</span>
    </div>

    <LoadingState v-if="loading && !data" />
    <ErrorState v-else-if="loadError" :error="loadError" @retry="reload" />
    <section v-else-if="data" class="data-panel permission-editor">
      <div class="permission-context">
        <label class="permission-context-field">
          <span><UsersRound :size="15" /> 群聊</span>
          <select v-model="selectedGroupId" :disabled="loading || Boolean(savingKey)">
            <option v-for="group in data.groups" :key="group.id" :value="group.id">
              {{ group.name }} / {{ group.account_name }}
            </option>
          </select>
          <small v-if="selectedGroup">
            {{ selectedGroupMemberCount }}
          </small>
        </label>

        <label class="permission-context-field">
          <span><UserRound :size="15" /> 成员例外</span>
          <select
            v-model="selectedMemberId"
            :disabled="loading || membersLoading || Boolean(savingKey) || !selectedGroupId"
          >
            <option value="">
              {{ membersLoading ? "正在加载成员" : "不查看成员例外" }}
            </option>
            <option v-for="member in members" :key="member.id" :value="member.id">
              {{ memberLabel(member) }}
            </option>
          </select>
          <small v-if="principalLoading">正在解析成员身份</small>
          <small v-else-if="principalError" class="permission-field-error">{{ principalError }}</small>
          <small v-else-if="membersError" class="permission-field-error">{{ membersError.message }}</small>
          <small v-else-if="selectedMember">
            {{ canWrite ? "正在编辑" : "正在查看" }} {{ memberLabel(selectedMember) }} 的成员例外
          </small>
          <small v-else>{{ canWrite ? "不选择成员时仅编辑群级规则" : "不选择成员时仅查看群级规则" }}</small>
        </label>
      </div>

      <ResourceToolbar
        v-model="search"
        :loading="loading || Boolean(savingKey)"
        :total="visiblePlugins.length"
        placeholder="搜索插件名称或资源 ID"
        @clear="search = ''"
        @refresh="reload"
      />

      <EmptyState
        v-if="!data.groups.length"
        title="暂无已发现群"
        detail="同步微信账号目录后才能配置群权限"
      >
        <template #icon><UsersRound :size="23" /></template>
      </EmptyState>
      <EmptyState
        v-else-if="!visiblePlugins.length"
        :title="search ? '没有匹配的插件' : '该工作区暂无插件资源'"
      >
        <template #icon><ShieldCheck :size="23" /></template>
      </EmptyState>
      <div v-else class="permission-editor-list">
        <div class="permission-editor-head" aria-hidden="true">
          <span>插件</span>
          <span>群级权限</span>
          <span>成员例外</span>
        </div>

        <article v-for="plugin in visiblePlugins" :key="plugin.id" class="permission-editor-row">
          <div class="permission-plugin">
            <strong>{{ plugin.name }}</strong>
            <code>{{ plugin.resource_id }}</code>
            <small v-if="plugin.retired">已退役或未在插件目录中</small>
          </div>

          <div class="permission-control-cell">
            <span class="permission-mobile-label">群级权限</span>
            <div
              class="permission-segmented"
              :class="{ 'permission-segmented--locked': lockedReason(plugin, null) }"
              :title="controlTitle(plugin, null)"
            >
              <button
                v-for="option in effects"
                :key="option.value"
                type="button"
                :class="[
                  `permission-option--${option.value.toLocaleLowerCase()}`,
                  { active: groupEffect(plugin) === option.value },
                ]"
                :aria-label="`${plugin.name} 群级权限：${option.label}`"
                :aria-pressed="groupEffect(plugin) === option.value"
                :disabled="controlDisabled(plugin, null)"
                @click="setEffect(plugin, option.value, null, false)"
              >
                {{ option.label }}
              </button>
            </div>
            <span v-if="lockedReason(plugin, null)" class="permission-lock-hint">
              <LockKeyhole :size="13" /> {{ lockedReason(plugin, null) }}
            </span>
          </div>

          <div class="permission-control-cell">
            <span class="permission-mobile-label">成员例外</span>
            <span v-if="!selectedMemberId" class="permission-member-placeholder">
              {{ canWrite ? "选择成员后配置" : "选择成员后查看" }}
            </span>
            <span v-else-if="principalLoading" class="permission-member-placeholder">正在读取规则</span>
            <span v-else-if="principalError || !principalResolved" class="permission-member-placeholder permission-field-error">
              无法读取成员例外，暂不可编辑
            </span>
            <template v-else>
              <div
                class="permission-segmented"
                :class="{
                  'permission-segmented--locked': lockedReason(plugin, memberPrincipal?.id || null),
                }"
                :title="memberControlTitle(plugin)"
              >
                <button
                  v-for="option in effects"
                  :key="option.value"
                  type="button"
                  :class="[
                    `permission-option--${option.value.toLocaleLowerCase()}`,
                    { active: memberEffect(plugin) === option.value },
                  ]"
                  :aria-label="`${plugin.name} 成员例外：${option.label}`"
                  :aria-pressed="memberEffect(plugin) === option.value"
                  :disabled="memberControlDisabled(plugin, option.value)"
                  @click="setEffect(plugin, option.value, memberPrincipal, true)"
                >
                  {{ option.label }}
                </button>
              </div>
              <span v-if="!memberPrincipal" class="permission-member-placeholder">
                尚无成员例外，当前继承群级权限
              </span>
              <span
                v-if="lockedReason(plugin, memberPrincipal?.id || null)"
                class="permission-lock-hint"
              >
                <LockKeyhole :size="13" />
                {{ lockedReason(plugin, memberPrincipal?.id || null) }}
              </span>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
