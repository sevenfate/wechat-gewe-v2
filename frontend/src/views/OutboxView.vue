<script setup lang="ts">
import { Ban, CheckCircle2, CircleAlert, Send, X } from "lucide-vue-next";
import { computed, onMounted, ref, watch } from "vue";

import { ApiError } from "@/api/client";
import { outboxApi, type OutboxMessage, type OutboxStatus } from "@/api/operations";
import { managementApi } from "@/api/resources";
import type { BotAccount } from "@/api/types";
import { hasPermission } from "@/auth/permissions";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDateTime } from "@/utils/format";

const statusOptions: Array<{ value: OutboxStatus | ""; label: string }> = [
  { value: "", label: "全部状态" },
  { value: "PENDING", label: "待发送" },
  { value: "CLAIMED", label: "已领取" },
  { value: "SENDING", label: "发送中" },
  { value: "SENT", label: "已发送" },
  { value: "FAILED_RETRYABLE", label: "等待重试" },
  { value: "FAILED_FINAL", label: "最终失败" },
  { value: "UNKNOWN", label: "结果未知" },
  { value: "CANCELLED", label: "已取消" },
];

const messages = ref<OutboxMessage[]>([]);
const accounts = ref<BotAccount[]>([]);
const total = ref<number | null>(null);
const nextOffset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const loadingMore = ref(false);
const error = ref<ApiError | null>(null);
const accountLabelsLoading = ref(false);
const accountLabelsUnavailable = ref(false);
const search = ref("");
const selectedStatus = ref<OutboxStatus | "">("");
const selectedAccountId = ref("");
const operation = ref<"cancel" | "sent" | "failed" | null>(null);
const selectedMessage = ref<OutboxMessage | null>(null);
const operationReason = ref("");
const operationError = ref<ApiError | null>(null);
const submitting = ref(false);
let loadEpoch = 0;
let accountLabelsEpoch = 0;

const canReadAccounts = computed(() =>
  hasPermission(authSession.state.user, "account.read"),
);
const canManage = computed(
  () =>
    authSession.state.user?.roles.includes("owner") === true ||
    authSession.state.user?.permissions.includes("outbox.manage") === true,
);

const accountNames = computed(
  () => new Map(accounts.value.map((account) => [account.id, account.nickname || account.wxid || account.app_id])),
);
const filteredMessages = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  if (!keyword) return messages.value;
  return messages.value.filter((message) =>
    [message.id, message.trace_id, message.target_wxid, message.action_type, payloadPreview(message)]
      .some((value) => value.toLocaleLowerCase().includes(keyword)),
  );
});
const isTruncated = computed(() => hasMore.value);

function payloadPreview(message: OutboxMessage): string {
  const text = message.payload.text;
  if (typeof text === "string") return text;
  return JSON.stringify(message.payload) || "{}";
}

function canCancel(status: OutboxStatus): boolean {
  return ["PENDING", "CLAIMED", "FAILED_RETRYABLE"].includes(status);
}

async function loadPage(offset: number, append: boolean) {
  const requestEpoch = ++loadEpoch;
  const botAccountId = selectedAccountId.value;
  const status = selectedStatus.value;
  if (append) loadingMore.value = true;
  else loading.value = true;
  error.value = null;
  try {
    const messageResult = await outboxApi.list({ botAccountId, status, offset });
    if (requestEpoch !== loadEpoch) return;
    if (append) {
      const existingIds = new Set(messages.value.map((message) => message.id));
      messages.value = [
        ...messages.value,
        ...messageResult.items.filter((message) => !existingIds.has(message.id)),
      ];
    } else {
      messages.value = messageResult.items;
    }
    total.value = messageResult.total;
    nextOffset.value = offset + messageResult.items.length;
    hasMore.value = messageResult.items.length > 0 && nextOffset.value < messageResult.total;
  } catch (caught) {
    if (requestEpoch !== loadEpoch) return;
    error.value = caught instanceof ApiError ? caught : new ApiError("读取发送队列失败", 0);
    if (!append) {
      messages.value = [];
      total.value = null;
      nextOffset.value = 0;
      hasMore.value = false;
    }
  } finally {
    if (requestEpoch === loadEpoch) {
      loading.value = false;
      loadingMore.value = false;
    }
  }
}

async function load() {
  await loadPage(0, false);
}

async function loadMore() {
  if (loading.value || loadingMore.value || !isTruncated.value) return;
  await loadPage(nextOffset.value, true);
}

async function loadAccountLabels() {
  const requestEpoch = ++accountLabelsEpoch;
  accounts.value = [];
  accountLabelsUnavailable.value = false;
  if (!canReadAccounts.value) return;

  accountLabelsLoading.value = true;
  try {
    const result = await managementApi.accounts.list();
    if (requestEpoch !== accountLabelsEpoch) return;
    accounts.value = result.items;
  } catch {
    if (requestEpoch !== accountLabelsEpoch) return;
    accountLabelsUnavailable.value = true;
  } finally {
    if (requestEpoch === accountLabelsEpoch) accountLabelsLoading.value = false;
  }
}

function openOperation(message: OutboxMessage, next: "cancel" | "sent" | "failed") {
  selectedMessage.value = message;
  operation.value = next;
  operationReason.value = "";
  operationError.value = null;
}

function closeOperation() {
  if (submitting.value) return;
  operation.value = null;
  selectedMessage.value = null;
  operationReason.value = "";
  operationError.value = null;
}

async function submitOperation() {
  const message = selectedMessage.value;
  const action = operation.value;
  const reason = operationReason.value.trim();
  if (!message || !action || !reason) return;
  submitting.value = true;
  operationError.value = null;
  try {
    if (action === "cancel") {
      await outboxApi.cancel(message.id, reason);
    } else {
      await outboxApi.reconcile(message.id, action === "sent" ? "SENT" : "FAILED_FINAL", reason);
    }
    submitting.value = false;
    closeOperation();
    await load();
  } catch (caught) {
    operationError.value = caught instanceof ApiError ? caught : new ApiError("操作失败", 0);
  } finally {
    submitting.value = false;
  }
}

watch([selectedStatus, selectedAccountId], () => void load());
onMounted(() => {
  void load();
  void loadAccountLabels();
});
</script>

<template>
  <div class="page-stack">
    <PageHeader title="发送队列" description="查看可靠发送状态，处理取消与结果未知消息" />

    <section
      v-if="!canReadAccounts || accountLabelsUnavailable"
      class="notice-band notice-band--neutral"
      role="status"
    >
      <CircleAlert :size="18" />
      <span>
        {{
          canReadAccounts
            ? "账号名称暂时不可用，账号列已退化为内部 ID。"
            : "当前角色没有 account.read 权限，账号列以内部 ID 展示。"
        }}
      </span>
    </section>

    <section v-if="isTruncated" class="notice-band notice-band--neutral" role="status">
      <CircleAlert :size="18" />
      <span>
        服务端共有 {{ total }} 条匹配记录，当前已加载 {{ messages.length }} 条；页面搜索仅覆盖已加载记录。
      </span>
    </section>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索目标、消息 ID 或 Trace"
        @clear="search = ''"
        @refresh="load"
      >
        <select
          v-if="canReadAccounts"
          v-model="selectedAccountId"
          class="filter-select"
          :disabled="accountLabelsLoading"
          aria-label="筛选微信账号"
        >
          <option value="">全部账号</option>
          <option v-for="account in accounts" :key="account.id" :value="account.id">
            {{ account.nickname || account.wxid || account.app_id }}
          </option>
        </select>
        <select v-model="selectedStatus" class="filter-select" aria-label="筛选发送状态">
          <option v-for="item in statusOptions" :key="item.value" :value="item.value">
            {{ item.label }}
          </option>
        </select>
      </ResourceToolbar>

      <LoadingState v-if="loading && !messages.length" />
      <ErrorState v-else-if="error && !messages.length" :error="error" @retry="load" />
      <EmptyState v-else-if="!filteredMessages.length" title="没有匹配的发送记录">
        <template #icon><Send :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table outbox-table">
          <thead>
            <tr>
              <th>目标与内容</th>
              <th>账号</th>
              <th>状态</th>
              <th>尝试</th>
              <th>错误码</th>
              <th>GeWe 结果</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="message in filteredMessages" :key="message.id">
              <td>
                <span class="stacked-cell">
                  <code>{{ message.target_wxid }}</code>
                  <small class="payload-preview" :title="payloadPreview(message)">
                    {{ payloadPreview(message) }}
                  </small>
                </span>
              </td>
              <td>{{ accountNames.get(message.bot_account_id) || message.bot_account_id }}</td>
              <td><StatusBadge :status="message.status" /></td>
              <td>
                <span class="stacked-cell">
                  <strong>{{ message.attempt_count }}</strong>
                  <small>{{ formatDateTime(message.last_attempt_started_at) }}</small>
                </span>
              </td>
              <td><code>{{ message.last_error_code || "-" }}</code></td>
              <td>
                <span class="stacked-cell provider-result">
                  <code>{{ message.provider_new_message_id || "-" }}</code>
                  <small v-if="message.provider_message_id">
                    msgId {{ message.provider_message_id }}
                  </small>
                </span>
              </td>
              <td>{{ formatDateTime(message.created_at) }}</td>
              <td>
                <div class="row-actions">
                  <button
                    v-if="canManage && canCancel(message.status)"
                    class="button button--danger"
                    type="button"
                    @click="openOperation(message, 'cancel')"
                  >
                    <Ban :size="15" />取消
                  </button>
                  <template v-else-if="canManage && message.status === 'UNKNOWN'">
                    <button
                      class="button button--secondary"
                      type="button"
                      @click="openOperation(message, 'sent')"
                    >
                      <CheckCircle2 :size="15" />确认为已发送
                    </button>
                    <button
                      class="button button--danger"
                      type="button"
                      @click="openOperation(message, 'failed')"
                    >
                      <CircleAlert :size="15" />确认为失败
                    </button>
                  </template>
                  <span v-else class="muted-text">-</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="messages.length && (isTruncated || error)" class="outbox-pagination">
        <span v-if="error" class="inline-error" role="alert">{{ error.message }}</span>
        <button
          v-if="isTruncated"
          class="button button--secondary"
          type="button"
          :disabled="loadingMore"
          @click="loadMore"
        >
          {{ loadingMore ? "正在加载" : "加载更多" }}
        </button>
      </div>
    </section>

    <div v-if="operation && selectedMessage" class="operation-dialog-backdrop" @click.self="closeOperation">
      <form class="operation-dialog" role="dialog" aria-modal="true" @submit.prevent="submitOperation">
        <header class="operation-dialog-header">
          <h3>
            {{ operation === "cancel" ? "取消发送" : operation === "sent" ? "确认已发送" : "确认发送失败" }}
          </h3>
          <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeOperation">
            <X :size="18" />
          </button>
        </header>
        <div class="operation-dialog-body">
          <span class="notice-band notice-band--neutral">
            <code>{{ selectedMessage.id }}</code>
          </span>
          <label class="field-control">
            <span>操作原因</span>
            <textarea v-model="operationReason" required maxlength="500" autofocus />
          </label>
          <div v-if="operationError" class="inline-error" role="alert">{{ operationError.message }}</div>
        </div>
        <footer class="operation-dialog-actions">
          <button class="button button--secondary" type="button" :disabled="submitting" @click="closeOperation">
            返回
          </button>
          <button class="button button--primary" type="submit" :disabled="submitting || !operationReason.trim()">
            {{ submitting ? "正在提交" : "确认" }}
          </button>
        </footer>
      </form>
    </div>
  </div>
</template>
