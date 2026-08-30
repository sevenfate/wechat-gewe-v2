<script setup lang="ts">
import { Check, CloudDownload, UserRound, X } from "lucide-vue-next";
import { computed, ref, shallowRef, watch } from "vue";

import { ApiError } from "@/api/client";
import { managementApi } from "@/api/resources";
import type { Contact } from "@/api/types";
import { wechatOperationsApi } from "@/api/wechat-operations";
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
import { formatDateTime } from "@/utils/format";
import "@/styles/wechat-operations.css";

const selectedAccountId = ref("");
const syncing = ref(false);
const actionError = shallowRef<ApiError | null>(null);
const feedback = ref("");
const {
  data: accountData,
  loading: accountsLoading,
  error: accountsError,
  reload: reloadAccounts,
} = useAsyncResource(() => managementApi.accounts.list());
const { items, total, loading, error, search, reload, clearSearch } = useListResource<Contact>(
  (keyword) =>
    selectedAccountId.value
      ? managementApi.contacts.list(selectedAccountId.value, keyword)
      : Promise.resolve({ items: [], total: 0, next_cursor: null }),
);

const selectedAccount = computed(() =>
  accountData.value?.items.find((account) => account.id === selectedAccountId.value),
);
const canSync = computed(() =>
  authSession.state.user?.roles.includes("owner") === true ||
  authSession.state.user?.permissions.includes("directory.sync") === true,
);

watch(accountData, (result) => {
  const accounts = result?.items || [];
  if (!accounts.some((account) => account.id === selectedAccountId.value)) {
    selectedAccountId.value = accounts[0]?.id || "";
  }
});

watch(selectedAccountId, (accountId) => {
  if (accountId) void reload();
});

function asApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError(fallback, 0, "UNKNOWN_ERROR");
}

async function syncDirectory() {
  if (!canSync.value || !selectedAccountId.value) return;
  syncing.value = true;
  actionError.value = null;
  feedback.value = "";
  try {
    const result = await wechatOperationsApi.directory.sync(selectedAccountId.value);
    feedback.value = `同步完成：发现 ${result.observed_contacts} 个联系人、${result.observed_chatrooms} 个群聊。`;
    await reload();
  } catch (caught) {
    actionError.value = asApiError(caught, "同步通讯录失败");
  } finally {
    syncing.value = false;
  }
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="联系人" description="按微信账号查看持久化通讯录与最近同步信息">
      <template #actions>
        <button
          class="button button--primary"
          type="button"
          :disabled="!canSync || !selectedAccountId || syncing || selectedAccount?.status === 'DISABLED'"
          :title="selectedAccount?.status === 'DISABLED' ? '账号已停用' : '从 GeWe 同步通讯录'"
          @click="syncDirectory"
        >
          <CloudDownload :class="{ spin: syncing }" :size="16" />
          {{ syncing ? "正在同步" : "同步通讯录" }}
        </button>
      </template>
    </PageHeader>

    <div v-if="feedback" class="wechat-feedback wechat-feedback--success" role="status">
      <Check :size="17" />
      <span>{{ feedback }}</span>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭提示" @click="feedback = ''">
        <X :size="15" />
      </button>
    </div>
    <div v-if="actionError" class="wechat-feedback wechat-feedback--error" role="alert">
      <span>{{ actionError.message }}</span>
      <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭错误" @click="actionError = null">
        <X :size="15" />
      </button>
    </div>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading || accountsLoading || syncing"
        :total="total"
        placeholder="搜索昵称、备注或 wxid"
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
        detail="连接微信账号后才能读取通讯录"
      >
        <template #icon><UserRound :size="23" /></template>
      </EmptyState>
      <LoadingState v-else-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的联系人' : '该账号暂无联系人数据'">
        <template #icon><UserRound :size="23" /></template>
        <template v-if="!search" #action>
          <button
            class="button button--primary"
            type="button"
            :disabled="!canSync || syncing || selectedAccount?.status === 'DISABLED'"
            @click="syncDirectory"
          >
            <CloudDownload :size="16" />同步通讯录
          </button>
        </template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>联系人</th>
              <th>备注</th>
              <th>类型</th>
              <th>状态</th>
              <th>最后同步</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="contact in items" :key="contact.id">
              <td>
                <IdentityCell
                  :name="contact.nickname"
                  :secondary="contact.external_id"
                  :avatar-url="contact.avatar_url"
                />
              </td>
              <td>{{ contact.remark || "-" }}</td>
              <td>{{ contact.contact_type || "-" }}</td>
              <td><StatusBadge :status="contact.active ? 'ACTIVE' : 'INACTIVE'" /></td>
              <td>{{ formatDateTime(contact.last_synced_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
