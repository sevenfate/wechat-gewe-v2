<script setup lang="ts">
import {
  Bot,
  Cable,
  Package,
  ShieldCheck,
} from "lucide-vue-next";
import { computed } from "vue";

import { managementApi } from "@/api/resources";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import { useAsyncResource } from "@/composables/useAsyncResource";
import { formatDateTime, formatInteger } from "@/utils/format";

const { data, loading, error, reload } = useAsyncResource(managementApi.overview);

const metrics = computed(() => {
  const overview = data.value;
  if (!overview) return [];
  return [
    overview.visibility.connections
      ? {
          label: "活动连接",
          value: `${formatInteger(overview.active_connections)} / ${formatInteger(overview.total_connections)}`,
          icon: Cable,
          tone: "green",
        }
      : null,
    overview.visibility.accounts
      ? {
          label: "在线账号",
          value: `${formatInteger(overview.online_accounts)} / ${formatInteger(overview.total_accounts)}`,
          icon: Bot,
          tone: "green",
        }
      : null,
    overview.visibility.plugins
      ? {
          label: "插件",
          value: formatInteger(overview.plugin_count),
          icon: Package,
          tone: "blue",
        }
      : null,
    overview.visibility.rules
      ? {
          label: "权限规则",
          value: formatInteger(overview.rule_count),
          icon: ShieldCheck,
          tone: "amber",
        }
      : null,
  ].filter((metric): metric is NonNullable<typeof metric> => metric !== null);
});

const isLimited = computed(() => Boolean(data.value && metrics.value.length < 4));
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
    <template v-else>
      <section v-if="isLimited" class="notice-band notice-band--neutral" role="status">
        <ShieldCheck :size="18" />
        <span>已按当前后台权限展示可读指标，未授权的数据不会发起请求。</span>
      </section>
      <EmptyState
        v-if="!metrics.length"
        title="当前权限下没有可读的总览指标"
        detail="你仍可通过左侧导航进入已授权的功能。"
      >
        <template #icon><ShieldCheck :size="23" /></template>
      </EmptyState>
    </template>
    <section v-if="!error && metrics.length" class="metrics-grid" aria-label="运营指标">
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

    <section v-if="!error && metrics.length" class="content-section">
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
