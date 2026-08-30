<script setup lang="ts">
import { CircleAlert, Eye, MessagesSquare, X } from "lucide-vue-next";
import { computed, onMounted, ref, watch } from "vue";

import { ApiError } from "@/api/client";
import {
  observabilityApi,
  type MessageDetail,
  type MessageSummary,
  type TraceView,
} from "@/api/observability";
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
import "@/styles/observability.css";
import { formatDateTime } from "@/utils/format";

const messages = ref<MessageSummary[]>([]);
const accounts = ref<BotAccount[]>([]);
const total = ref<number | null>(null);
const nextOffset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const loadingMore = ref(false);
const accountLabelsLoading = ref(false);
const accountLabelsUnavailable = ref(false);
const detailLoading = ref(false);
const error = ref<ApiError | null>(null);
const detailError = ref<ApiError | null>(null);
const search = ref("");
const selectedAccountId = ref("");
const selectedStatus = ref("");
const selectedConversationType = ref("");
const trace = ref<TraceView | null>(null);
const messageDetail = ref<MessageDetail | null>(null);
let loadEpoch = 0;
let accountLabelsEpoch = 0;
let detailEpoch = 0;

const canReadAccounts = computed(() =>
  hasPermission(authSession.state.user, "account.read"),
);
const canReadTrace = computed(
  () =>
    authSession.state.user?.roles.includes("owner") === true ||
    authSession.state.user?.permissions.includes("audit.read") === true,
);
const accountNames = computed(
  () => new Map(accounts.value.map((account) => [account.id, account.nickname || account.wxid || account.app_id])),
);
const filtered = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  if (!keyword) return messages.value;
  return messages.value.filter((message) =>
    [message.text_preview, message.actor_wxid, message.conversation_id, message.trace_id]
      .some((value) => value?.toLocaleLowerCase().includes(keyword)),
  );
});
const openedMessage = computed(() => trace.value?.message || messageDetail.value);

async function loadPage(offset: number, append: boolean) {
  const requestEpoch = ++loadEpoch;
  const botAccountId = selectedAccountId.value;
  const status = selectedStatus.value;
  const conversationType = selectedConversationType.value;
  if (append) loadingMore.value = true;
  else loading.value = true;
  error.value = null;
  try {
    const result = await observabilityApi.messages({
      botAccountId,
      status,
      conversationType,
      offset,
    });
    if (requestEpoch !== loadEpoch) return;
    if (append) {
      const existingIds = new Set(messages.value.map((message) => message.id));
      messages.value = [
        ...messages.value,
        ...result.items.filter((message) => !existingIds.has(message.id)),
      ];
    } else {
      messages.value = result.items;
    }
    total.value = result.total;
    nextOffset.value = offset + result.items.length;
    hasMore.value = result.items.length > 0 && nextOffset.value < result.total;
  } catch (caught) {
    if (requestEpoch !== loadEpoch) return;
    error.value = caught instanceof ApiError ? caught : new ApiError("读取消息中心失败", 0);
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
  if (loading.value || loadingMore.value || !hasMore.value) return;
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

async function openMessage(message: MessageSummary) {
  const requestEpoch = ++detailEpoch;
  trace.value = null;
  messageDetail.value = null;
  detailError.value = null;
  detailLoading.value = true;
  try {
    if (canReadTrace.value) {
      const result = await observabilityApi.trace(message.trace_id);
      if (requestEpoch !== detailEpoch) return;
      trace.value = result;
    } else {
      const result = await observabilityApi.message(message.id);
      if (requestEpoch !== detailEpoch) return;
      messageDetail.value = result;
    }
  } catch (caught) {
    if (requestEpoch !== detailEpoch) return;
    detailError.value = caught instanceof ApiError ? caught : new ApiError("读取消息详情失败", 0);
  } finally {
    if (requestEpoch === detailEpoch) detailLoading.value = false;
  }
}

function closeMessage() {
  detailEpoch += 1;
  detailLoading.value = false;
  trace.value = null;
  messageDetail.value = null;
  detailError.value = null;
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

watch([selectedAccountId, selectedStatus, selectedConversationType], () => void load());
onMounted(() => {
  void load();
  void loadAccountLabels();
});
</script>

<template>
  <div class="page-stack">
    <PageHeader title="消息与 Trace" description="查询标准事件、权限决策、插件审计与发送结果" />

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

    <section v-if="hasMore" class="notice-band notice-band--neutral" role="status">
      <CircleAlert :size="18" />
      <span>服务端共有 {{ total }} 条匹配消息，当前已加载 {{ messages.length }} 条。</span>
    </section>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索消息、会话、成员或 Trace"
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
        <select v-model="selectedConversationType" class="filter-select" aria-label="筛选会话类型">
          <option value="">全部会话</option>
          <option value="GROUP">群聊</option>
          <option value="PRIVATE">私聊</option>
          <option value="SYSTEM">系统</option>
          <option value="UNKNOWN">未知</option>
        </select>
        <select v-model="selectedStatus" class="filter-select" aria-label="筛选处理状态">
          <option value="">全部状态</option>
          <option value="RECEIVED">已接收</option>
          <option value="NORMALIZED">已标准化</option>
          <option value="DISPATCHING">分发中</option>
          <option value="DISPATCHED">已分发</option>
          <option value="FAILED">失败</option>
          <option value="IGNORED_SELF">忽略自发</option>
        </select>
      </ResourceToolbar>

      <LoadingState v-if="loading && !messages.length" />
      <ErrorState v-else-if="error && !messages.length" :error="error" @retry="load" />
      <EmptyState v-else-if="!filtered.length" title="没有匹配的消息">
        <template #icon><MessagesSquare :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table messages-table">
          <thead>
            <tr>
              <th>消息</th>
              <th>账号</th>
              <th>会话</th>
              <th>处理状态</th>
              <th>事件类型</th>
              <th>接收时间</th>
              <th>Trace</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="message in filtered" :key="message.id">
              <td>
                <span class="message-preview">
                  <strong>{{ message.text_preview || "（无文本内容）" }}</strong>
                  <small>{{ message.actor_wxid || "未知发送者" }}</small>
                </span>
              </td>
              <td>{{ message.bot_account_id ? accountNames.get(message.bot_account_id) || message.bot_account_id : "-" }}</td>
              <td>
                <span class="stacked-cell">
                  <span>{{ message.conversation_type }}</span>
                  <code>{{ message.conversation_id || "-" }}</code>
                </span>
              </td>
              <td><StatusBadge :status="message.inbox_status" /></td>
              <td><code>{{ message.event_type }}</code></td>
              <td>{{ formatDateTime(message.received_at) }}</td>
              <td>
                <button
                  class="icon-button icon-button--small"
                  type="button"
                  title="查看详情"
                  aria-label="查看详情"
                  @click="openMessage(message)"
                >
                  <Eye :size="15" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="messages.length && (hasMore || error)" class="list-pagination">
        <span v-if="error" class="inline-error" role="alert">{{ error.message }}</span>
        <button
          v-if="hasMore"
          class="button button--secondary"
          type="button"
          :disabled="loadingMore"
          @click="loadMore"
        >
          {{ loadingMore ? "正在加载" : "加载更多" }}
        </button>
      </div>
    </section>

    <div
      v-if="detailLoading || openedMessage || detailError"
      class="operation-dialog-backdrop"
      @click.self="closeMessage"
    >
      <section class="operation-dialog trace-dialog" role="dialog" aria-modal="true">
        <header class="operation-dialog-header">
          <h3>消息 Trace</h3>
          <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeMessage">
            <X :size="18" />
          </button>
        </header>
        <div class="operation-dialog-body">
          <LoadingState v-if="detailLoading" />
          <ErrorState v-else-if="detailError" :error="detailError" />
          <template v-else-if="openedMessage">
            <div class="trace-summary">
              <span><small>Trace ID</small><code>{{ openedMessage.trace_id }}</code></span>
              <span><small>会话</small><code>{{ openedMessage.conversation_id || "-" }}</code></span>
              <span><small>发送者</small><code>{{ openedMessage.actor_wxid || "-" }}</code></span>
            </div>

            <section v-if="trace?.policy_decisions.length" class="trace-section">
              <h4>权限决策</h4>
              <div class="trace-timeline">
                <div v-for="item in trace.policy_decisions" :key="item.id" class="trace-entry">
                  <StatusBadge :status="item.effect" />
                  <span class="trace-entry-copy">
                    <strong>{{ item.reason }}</strong>
                    <small>Policy v{{ item.policy_version }}</small>
                  </span>
                  <small>{{ formatDateTime(item.created_at) }}</small>
                </div>
              </div>
            </section>

            <section v-if="trace?.audit_events.length" class="trace-section">
              <h4>插件与系统审计</h4>
              <div class="trace-timeline">
                <div v-for="item in trace.audit_events" :key="item.id" class="trace-entry">
                  <StatusBadge :status="item.result" />
                  <span class="trace-entry-copy">
                    <strong>{{ item.action }}</strong>
                    <small>{{ item.object_type }} · {{ item.object_id }}</small>
                  </span>
                  <small>{{ formatDateTime(item.created_at) }}</small>
                </div>
              </div>
            </section>

            <section v-if="trace?.outbox_messages.length" class="trace-section">
              <h4>发送结果</h4>
              <div class="trace-timeline">
                <div v-for="item in trace.outbox_messages" :key="item.id" class="trace-entry">
                  <StatusBadge :status="item.status" />
                  <span class="trace-entry-copy">
                    <strong>{{ item.target_wxid }}</strong>
                    <small>{{ item.action_type }} · 尝试 {{ item.attempt_count }} 次</small>
                  </span>
                  <small>{{ item.last_error_code || formatDateTime(item.updated_at) }}</small>
                </div>
              </div>
            </section>

            <details class="raw-details">
              <summary>标准事件与原始回调</summary>
              <pre>{{ prettyJson({ content: openedMessage.content, raw_payload: openedMessage.raw_payload }) }}</pre>
            </details>
          </template>
        </div>
      </section>
    </div>
  </div>
</template>
