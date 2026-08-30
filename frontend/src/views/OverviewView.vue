<script setup lang="ts">
import {
  Activity,
  BellRing,
  Bot,
  CircleDollarSign,
  CircleX,
  Inbox,
  MessagesSquare,
} from "lucide-vue-next";
import { computed } from "vue";

import { managementApi } from "@/api/resources";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useAsyncResource } from "@/composables/useAsyncResource";
import { formatCurrency, formatDateTime, formatInteger } from "@/utils/format";

const { data, loading, error, reload } = useAsyncResource(managementApi.overview);

const hasMetrics = computed(() => {
  if (!data.value) return false;
  return [
    data.value.online_accounts,
    data.value.total_accounts,
    data.value.messages_today,
    data.value.queue_depth,
    data.value.failures_today,
    data.value.pending_approvals,
    data.value.agent_cost_today?.amount,
    data.value.active_alerts,
  ].some((value) => value !== null && value !== undefined);
});

const metrics = computed(() => [
  {
    label: "在线账号",
    value:
      data.value?.online_accounts === null || data.value?.online_accounts === undefined
        ? "-"
        : `${formatInteger(data.value.online_accounts)} / ${formatInteger(data.value.total_accounts)}`,
    icon: Bot,
    tone: "green",
  },
  {
    label: "今日消息",
    value: formatInteger(data.value?.messages_today),
    icon: MessagesSquare,
    tone: "blue",
  },
  {
    label: "队列积压",
    value: formatInteger(data.value?.queue_depth),
    icon: Inbox,
    tone: "amber",
  },
  {
    label: "今日失败",
    value: formatInteger(data.value?.failures_today),
    icon: CircleX,
    tone: "red",
  },
  {
    label: "待审批",
    value: formatInteger(data.value?.pending_approvals),
    icon: Activity,
    tone: "violet",
  },
  {
    label: "Agent 成本",
    value: formatCurrency(data.value?.agent_cost_today?.amount, data.value?.agent_cost_today?.currency),
    icon: CircleDollarSign,
    tone: "cyan",
  },
  {
    label: "活动告警",
    value: formatInteger(data.value?.active_alerts),
    icon: BellRing,
    tone: "orange",
  },
]);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="运营总览" description="账号、消息处理与运行风险的当前状态">
      <template #actions>
        <span v-if="data?.updated_at" class="updated-at">更新于 {{ formatDateTime(data.updated_at) }}</span>
      </template>
    </PageHeader>

    <LoadingState v-if="loading && !data" />
    <ErrorState v-else-if="error" :error="error" @retry="reload" />
    <EmptyState v-else-if="!hasMetrics" title="暂无概览数据" detail="管理 API 尚未返回运营指标" />
    <section v-else class="metrics-grid" aria-label="运营指标">
      <article v-for="metric in metrics" :key="metric.label" class="metric-card">
        <span class="metric-icon" :class="`metric-icon--${metric.tone}`">
          <component :is="metric.icon" :size="19" stroke-width="1.8" />
        </span>
        <div class="metric-copy">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value }}</strong>
        </div>
      </article>
    </section>

    <section class="content-section">
      <div class="section-heading">
        <div>
          <h3>需要关注</h3>
          <p>失败任务、离线账号和权限审批将在此聚合</p>
        </div>
      </div>
      <EmptyState title="当前没有可展示的关注项" />
    </section>
  </div>
</template>
