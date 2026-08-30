<script setup lang="ts">
import { Check, CloudDownload, Eye, RefreshCw, Search, UserMinus, UsersRound, X } from "lucide-vue-next";
import { computed, ref, shallowRef, watch } from "vue";

import { ApiError } from "@/api/client";
import { managementApi } from "@/api/resources";
import type { DiscoveredGroup } from "@/api/types";
import {
  wechatOperationsApi,
  type ChatroomMember,
  type MembershipSyncResult,
} from "@/api/wechat-operations";
import { hasAllPermissions } from "@/auth/permissions";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import IdentityCell from "@/components/IdentityCell.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAsyncResource } from "@/composables/useAsyncResource";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime, formatInteger } from "@/utils/format";
import "@/styles/wechat-operations.css";

const selectedAccountId = ref("");
const directorySyncing = ref(false);
const activeAction = ref("");
const actionError = shallowRef<ApiError | null>(null);
const feedback = ref("");
const selectedGroup = shallowRef<DiscoveredGroup | null>(null);
const members = shallowRef<ChatroomMember[]>([]);
const memberTotal = ref(0);
const membersLoading = ref(false);
const membersError = shallowRef<ApiError | null>(null);
const includeLeft = ref(false);
const memberSearch = ref("");
const departureMember = shallowRef<ChatroomMember | null>(null);
const departureReason = ref("");
const departureSubmitting = ref(false);
const departureError = shallowRef<ApiError | null>(null);
let membersRequestEpoch = 0;

const canBrowseDirectory = computed(() =>
  hasAllPermissions(authSession.state.user, ["directory.read", "account.read"]),
);
const directoryAccessDetail = computed(() => {
  const user = authSession.state.user;
  const missing = ["directory.read", "account.read"].filter(
    (permission) => !hasAllPermissions(user, [permission]),
  );
  return `此页面需要 directory.read 和 account.read 权限，当前缺少 ${missing.join("、")}。`;
});

const {
  data: accountData,
  loading: accountsLoading,
  error: accountsError,
  reload: reloadAccounts,
} = useAsyncResource(() =>
  canBrowseDirectory.value
    ? managementApi.accounts.list()
    : Promise.resolve({ items: [], total: 0, next_cursor: null }),
);
const { items, total, loading, error, search, reload, clearSearch } = useListResource<DiscoveredGroup>(
  (keyword) =>
    selectedAccountId.value
      ? managementApi.groups.list(selectedAccountId.value, keyword)
      : Promise.resolve({ items: [], total: 0, next_cursor: null }),
);

const selectedAccount = computed(() =>
  accountData.value?.items.find((account) => account.id === selectedAccountId.value),
);
const canSync = computed(() =>
  authSession.state.user?.roles.includes("owner") === true ||
  authSession.state.user?.permissions.includes("directory.sync") === true,
);
const canConfirmDeparture = computed(() => {
  const user = authSession.state.user;
  return Boolean(
    user?.roles.includes("owner") ||
      (user?.permissions.includes("directory.sync") && user.permissions.includes("policy.write")),
  );
});
const filteredMembers = computed(() => {
  const keyword = memberSearch.value.trim().toLocaleLowerCase();
  if (!keyword) return members.value;
  return members.value.filter((member) =>
    [member.display_name, member.nickname, member.member_wxid, member.inviter_wxid]
      .some((value) => value?.toLocaleLowerCase().includes(keyword)),
  );
});

watch(accountData, (result) => {
  const accounts = result?.items || [];
  if (!accounts.some((account) => account.id === selectedAccountId.value)) {
    selectedAccountId.value = accounts[0]?.id || "";
  }
});

watch(selectedAccountId, (accountId) => {
  closeMembers();
  if (accountId) void reload();
});

watch(includeLeft, () => {
  if (selectedGroup.value) void loadMembers();
});

function asApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError(fallback, 0, "UNKNOWN_ERROR");
}

function isGroupBusy(groupId: string): boolean {
  return activeAction.value.startsWith(`${groupId}:`);
}

function memberName(member: ChatroomMember): string {
  return member.display_name || member.nickname || member.member_wxid;
}

function memberInitial(member: ChatroomMember): string {
  return memberName(member).trim().slice(0, 1).toUpperCase() || "?";
}

async function syncDirectory() {
  if (!canSync.value || !selectedAccountId.value) return;
  directorySyncing.value = true;
  actionError.value = null;
  feedback.value = "";
  try {
    const result = await wechatOperationsApi.directory.sync(selectedAccountId.value);
    feedback.value = `群目录同步完成：发现 ${result.observed_chatrooms} 个群聊，同时更新 ${result.observed_contacts} 个联系人。`;
    await reload();
  } catch (caught) {
    actionError.value = asApiError(caught, "同步群目录失败");
  } finally {
    directorySyncing.value = false;
  }
}

function syncMessage(result: MembershipSyncResult): string {
  const retained = result.retained_unseen_active_members;
  return `成员同步完成：本次观察到 ${result.observed_members} 人${
    retained ? `，保留 ${retained} 位本次未返回的现有成员` : ""
  }。${result.snapshot_complete ? "快照完整。" : "上游未声明完整快照，因此不会自动标记未返回成员离群。"}`;
}

async function syncMembers(group: DiscoveredGroup) {
  if (!canSync.value) return;
  activeAction.value = `${group.id}:sync`;
  actionError.value = null;
  feedback.value = "";
  try {
    const result = await wechatOperationsApi.directory.syncMembers(group.id);
    feedback.value = syncMessage(result);
    await reload();
    if (selectedGroup.value?.id === group.id) {
      selectedGroup.value = items.value.find((item) => item.id === group.id) || selectedGroup.value;
      await loadMembers();
    }
  } catch (caught) {
    const apiError = asApiError(caught, "同步群成员失败");
    actionError.value = apiError;
    if (selectedGroup.value?.id === group.id) membersError.value = apiError;
  } finally {
    activeAction.value = "";
  }
}

async function openMembers(group: DiscoveredGroup) {
  const resetIncludeLeft = includeLeft.value;
  selectedGroup.value = group;
  includeLeft.value = false;
  memberSearch.value = "";
  members.value = [];
  memberTotal.value = 0;
  if (!resetIncludeLeft) await loadMembers();
}

function closeMembers() {
  membersRequestEpoch += 1;
  membersLoading.value = false;
  selectedGroup.value = null;
  members.value = [];
  memberTotal.value = 0;
  membersError.value = null;
  memberSearch.value = "";
  includeLeft.value = false;
  departureMember.value = null;
  departureReason.value = "";
  departureError.value = null;
}

async function loadMembers() {
  const group = selectedGroup.value;
  if (!group) return;
  const requestEpoch = ++membersRequestEpoch;
  const requestedIncludeLeft = includeLeft.value;
  membersLoading.value = true;
  membersError.value = null;
  try {
    const result = await wechatOperationsApi.directory.listMembers(
      group.id,
      requestedIncludeLeft,
    );
    if (
      requestEpoch !== membersRequestEpoch ||
      selectedGroup.value?.id !== group.id ||
      includeLeft.value !== requestedIncludeLeft
    ) {
      return;
    }
    members.value = result.items;
    memberTotal.value = result.total;
  } catch (caught) {
    if (
      requestEpoch !== membersRequestEpoch ||
      selectedGroup.value?.id !== group.id ||
      includeLeft.value !== requestedIncludeLeft
    ) {
      return;
    }
    membersError.value = asApiError(caught, "读取群成员失败");
    members.value = [];
    memberTotal.value = 0;
  } finally {
    if (requestEpoch === membersRequestEpoch) membersLoading.value = false;
  }
}

function openDepartureConfirmation(member: ChatroomMember) {
  if (!canConfirmDeparture.value || !member.active) return;
  departureMember.value = member;
  departureReason.value = "";
  departureError.value = null;
}

function closeDepartureConfirmation() {
  if (departureSubmitting.value) return;
  departureMember.value = null;
  departureReason.value = "";
  departureError.value = null;
}

async function confirmDeparture() {
  const group = selectedGroup.value;
  const member = departureMember.value;
  const reason = departureReason.value.trim();
  if (!group || !member || !canConfirmDeparture.value || !reason) return;

  departureSubmitting.value = true;
  departureError.value = null;
  try {
    await wechatOperationsApi.directory.markMembershipLeft(group.id, member.id, {
      membershipEpoch: member.membership_epoch,
      reason,
    });
    feedback.value = `${memberName(member)} 已确认离群，第 ${member.membership_epoch} 次成员关系已关闭。`;
    departureMember.value = null;
    departureReason.value = "";
    await loadMembers();
  } catch (caught) {
    departureError.value = asApiError(caught, "确认成员离群失败");
  } finally {
    departureSubmitting.value = false;
  }
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="已发现群" description="按微信账号查看同步或消息中发现的群聊">
      <template #actions>
        <button
          v-if="canBrowseDirectory"
          class="button button--primary"
          type="button"
          :disabled="!canSync || !selectedAccountId || directorySyncing || selectedAccount?.status === 'DISABLED'"
          :title="selectedAccount?.status === 'DISABLED' ? '账号已停用' : '从 GeWe 同步群目录'"
          @click="syncDirectory"
        >
          <CloudDownload :class="{ spin: directorySyncing }" :size="16" />
          {{ directorySyncing ? "正在同步" : "同步群目录" }}
        </button>
      </template>
    </PageHeader>

    <EmptyState
      v-if="!canBrowseDirectory"
      title="当前角色无法读取群目录"
      :detail="directoryAccessDetail"
    >
      <template #icon><UsersRound :size="23" /></template>
    </EmptyState>

    <section v-if="canBrowseDirectory" class="notice-band">
      <UsersRound :size="18" />
      <span>此列表不代表微信账号加入过的全部历史群；成员接口返回的快照也可能不完整。</span>
    </section>

    <div v-if="canBrowseDirectory && feedback" class="wechat-feedback wechat-feedback--success" role="status">
      <Check :size="17" />
      <span>{{ feedback }}</span>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭提示" @click="feedback = ''">
        <X :size="15" />
      </button>
    </div>
    <div v-if="canBrowseDirectory && actionError" class="wechat-feedback wechat-feedback--error" role="alert">
      <span>{{ actionError.message }}</span>
      <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭错误" @click="actionError = null">
        <X :size="15" />
      </button>
    </div>

    <section v-if="canBrowseDirectory" class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading || accountsLoading || directorySyncing"
        :total="total"
        placeholder="搜索群名或 chatroom ID"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      >
        <label class="account-filter">
          <span>微信账号</span>
          <select v-model="selectedAccountId" :disabled="accountsLoading">
            <option v-for="account in accountData?.items || []" :key="account.id" :value="account.id">
              {{ account.nickname || account.wxid || account.app_id }}
            </option>
          </select>
        </label>
      </ResourceToolbar>

      <LoadingState v-if="accountsLoading && !accountData" />
      <ErrorState v-else-if="accountsError" :error="accountsError" @retry="reloadAccounts" />
      <EmptyState
        v-else-if="!accountData?.items.length"
        title="暂无微信账号"
        detail="连接微信账号后才能读取群目录"
      >
        <template #icon><UsersRound :size="23" /></template>
      </EmptyState>
      <LoadingState v-else-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的群' : '该账号暂无已发现群'">
        <template #icon><UsersRound :size="23" /></template>
        <template v-if="!search" #action>
          <button
            class="button button--primary"
            type="button"
            :disabled="!canSync || directorySyncing || selectedAccount?.status === 'DISABLED'"
            @click="syncDirectory"
          >
            <CloudDownload :size="16" />同步群目录
          </button>
        </template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table wechat-group-table">
          <thead>
            <tr>
              <th>群聊</th>
              <th>群主</th>
              <th>成员</th>
              <th>发现来源</th>
              <th>目录状态</th>
              <th>最后同步</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="group in items" :key="group.id">
              <td><IdentityCell :name="group.name" :secondary="group.chatroom_id" square /></td>
              <td><code>{{ group.owner_wxid || "-" }}</code></td>
              <td>{{ formatInteger(group.member_count) }}</td>
              <td>{{ group.discovered_from || "-" }}</td>
              <td><StatusBadge :status="group.placeholder ? 'PLACEHOLDER' : 'SYNCED'" /></td>
              <td>{{ formatDateTime(group.last_synced_at) }}</td>
              <td>
                <div class="wechat-row-actions">
                  <button class="button button--secondary" type="button" @click="openMembers(group)">
                    <Eye :size="15" />成员
                  </button>
                  <button
                    class="button button--secondary"
                    type="button"
                    :disabled="!canSync || selectedAccount?.status === 'DISABLED' || isGroupBusy(group.id)"
                    @click="syncMembers(group)"
                  >
                    <RefreshCw :class="{ spin: isGroupBusy(group.id) }" :size="15" />同步成员
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <Teleport to="body">
      <div v-if="selectedGroup" class="wechat-drawer-backdrop" @click.self="closeMembers">
        <aside class="wechat-drawer" role="dialog" aria-modal="true" aria-labelledby="member-drawer-title">
          <header class="wechat-drawer-header">
            <div>
              <p>群成员</p>
              <h3 id="member-drawer-title">{{ selectedGroup.name || selectedGroup.chatroom_id }}</h3>
              <small>{{ selectedGroup.chatroom_id }}</small>
            </div>
            <button class="icon-button" type="button" aria-label="关闭成员列表" title="关闭" @click="closeMembers">
              <X :size="19" />
            </button>
          </header>

          <div class="wechat-drawer-toolbar">
            <label class="wechat-member-search">
              <Search :size="16" />
              <input v-model="memberSearch" type="search" placeholder="搜索昵称或 wxid" aria-label="搜索群成员" />
            </label>
            <span>{{ filteredMembers.length }} / {{ memberTotal }}</span>
            <button
              class="icon-button"
              type="button"
              :disabled="membersLoading"
              aria-label="刷新成员列表"
              title="刷新成员列表"
              @click="loadMembers"
            >
              <RefreshCw :class="{ spin: membersLoading }" :size="16" />
            </button>
          </div>

          <div class="wechat-drawer-options">
            <label class="wechat-check-option">
              <input v-model="includeLeft" type="checkbox" />
              <span>包含已离群记录</span>
            </label>
            <button
              class="button button--primary"
              type="button"
              :disabled="!canSync || selectedAccount?.status === 'DISABLED' || isGroupBusy(selectedGroup.id)"
              @click="syncMembers(selectedGroup)"
            >
              <RefreshCw :class="{ spin: isGroupBusy(selectedGroup.id) }" :size="15" />
              同步成员
            </button>
          </div>

          <div class="wechat-drawer-content">
            <LoadingState v-if="membersLoading && !members.length" />
            <div v-else-if="membersError" class="wechat-drawer-error" role="alert">
              <strong>成员列表读取失败</strong>
              <span>{{ membersError.message }}</span>
              <button class="button button--secondary" type="button" @click="loadMembers">重试</button>
            </div>
            <EmptyState v-else-if="!filteredMembers.length" :title="memberSearch ? '没有匹配的成员' : '暂无群成员记录'">
              <template #icon><UsersRound :size="23" /></template>
            </EmptyState>
            <div v-else class="wechat-member-list">
              <article v-for="member in filteredMembers" :key="member.id" class="wechat-member-item">
                <span class="wechat-member-avatar">{{ memberInitial(member) }}</span>
                <span class="wechat-member-copy">
                  <strong>{{ memberName(member) }}</strong>
                  <code>{{ member.member_wxid }}</code>
                  <small v-if="member.display_name && member.nickname && member.display_name !== member.nickname">
                    微信昵称：{{ member.nickname }}
                  </small>
                  <small v-if="member.inviter_wxid">邀请人：{{ member.inviter_wxid }}</small>
                </span>
                <span class="wechat-member-meta">
                  <StatusBadge :status="member.active ? 'ACTIVE' : 'INACTIVE'" />
                  <small>第 {{ member.membership_epoch }} 次加入</small>
                  <small>{{ formatDateTime(member.joined_at) }}</small>
                  <small v-if="member.left_at">离群：{{ formatDateTime(member.left_at) }}</small>
                  <button
                    v-if="member.active && canConfirmDeparture"
                    class="button button--danger"
                    type="button"
                    @click="openDepartureConfirmation(member)"
                  >
                    <UserMinus :size="14" />确认离群
                  </button>
                </span>
              </article>
            </div>
          </div>
        </aside>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="departureMember"
        class="wechat-dialog-backdrop membership-departure-backdrop"
        @click.self="closeDepartureConfirmation"
      >
        <form class="wechat-dialog" role="dialog" aria-modal="true" @submit.prevent="confirmDeparture">
          <header class="wechat-dialog-header">
            <div>
              <h3>确认成员已离群</h3>
              <p>此操作会关闭当前成员关系，旧成员权限不会在重新入群后恢复。</p>
            </div>
            <button
              class="icon-button"
              type="button"
              :disabled="departureSubmitting"
              aria-label="关闭确认框"
              title="关闭"
              @click="closeDepartureConfirmation"
            >
              <X :size="18" />
            </button>
          </header>
          <div class="wechat-dialog-body">
            <span class="notice-band notice-band--neutral membership-departure-summary">
              <strong>{{ memberName(departureMember) }}</strong>
              <code>{{ departureMember.member_wxid }}</code>
              <small>当前为第 {{ departureMember.membership_epoch }} 次加入</small>
            </span>
            <label class="field-control">
              <span>确认理由</span>
              <textarea
                v-model="departureReason"
                required
                maxlength="500"
                autofocus
                placeholder="填写人工确认依据"
              />
            </label>
            <div v-if="departureError" class="inline-error" role="alert">
              {{ departureError.message }}
            </div>
          </div>
          <footer class="wechat-dialog-actions">
            <button
              class="button button--secondary"
              type="button"
              :disabled="departureSubmitting"
              @click="closeDepartureConfirmation"
            >
              返回
            </button>
            <button
              class="button button--danger"
              type="submit"
              :disabled="departureSubmitting || !departureReason.trim()"
            >
              <UserMinus :size="15" />
              {{ departureSubmitting ? "正在确认" : "确认离群" }}
            </button>
          </footer>
        </form>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.membership-departure-backdrop {
  z-index: 100;
}

.membership-departure-summary {
  display: grid;
  align-items: start;
  gap: 4px;
}

.membership-departure-summary code,
.membership-departure-summary small {
  font-size: 10px;
}
</style>
