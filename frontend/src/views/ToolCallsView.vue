<script setup lang="ts">
import { Braces, CircleAlert, Eye, RefreshCw, Wrench, X } from "lucide-vue-next";
import { computed, onMounted, ref, watch } from "vue";

import { ApiError } from "@/api/client";
import { toolBridgeApi, type ToolCall, type ToolCallStatus } from "@/api/tool-bridge";
import { hasPermission } from "@/auth/permissions";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDateTime } from "@/utils/format";
import "@/styles/tool-calls.css";

const statuses: Array<{ value: ToolCallStatus | ""; label: string }> = [
  { value: "", label: "全部状态" }, { value: "RECEIVED", label: "已接收" },
  { value: "AUTHORIZED", label: "已授权" }, { value: "EXECUTING", label: "执行中" },
  { value: "SUCCEEDED", label: "成功" }, { value: "DENIED", label: "已拒绝" },
  { value: "FAILED_RETRYABLE", label: "等待重试" }, { value: "FAILED_FINAL", label: "最终失败" },
  { value: "CANCELLED", label: "已取消" }, { value: "UNKNOWN", label: "未知" },
];
const calls = ref<ToolCall[]>([]);
const total = ref<number | null>(null);
const loading = ref(false);
const error = ref<ApiError | null>(null);
const search = ref("");
const selectedStatus = ref<ToolCallStatus | "">("");
const selectedCall = ref<ToolCall | null>(null);
const canRead = computed(() => hasPermission(authSession.state.user, "audit.read"));
const filteredCalls = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  if (!keyword) return calls.value;
  return calls.value.filter((call) =>
    [call.id, call.external_tool_call_id, call.tool_name, call.trace_id, call.error_code || ""]
      .some((value) => value.toLocaleLowerCase().includes(keyword)),
  );
});
let loadEpoch = 0;

async function load() {
  const epoch = ++loadEpoch;
  loading.value = true; error.value = null;
  try {
    const result = await toolBridgeApi.list({ status: selectedStatus.value, limit: 100 });
    if (epoch !== loadEpoch) return;
    calls.value = result.items; total.value = result.total;
  } catch (caught) {
    if (epoch !== loadEpoch) return;
    error.value = caught instanceof ApiError ? caught : new ApiError("读取 Tool 调用记录失败", 0);
    calls.value = []; total.value = null;
  } finally { if (epoch === loadEpoch) loading.value = false; }
}
function closeDetails() { selectedCall.value = null; }
function json(value: unknown): string { return JSON.stringify(value, null, 2) || "{}"; }
watch(selectedStatus, () => void load());
onMounted(() => { if (canRead.value) void load(); });
</script>

<template>
  <div class="page-stack">
    <PageHeader title="Tool 调用审计" description="只读查看插件与 MaiBot Tool 的调用状态、权限结果和执行记录" />
    <section v-if="!canRead" class="notice-band notice-band--neutral" role="status">
      <CircleAlert :size="18" /><span>当前账号没有 audit.read 权限，无法查看 Tool 调用记录。</span>
    </section>
    <section v-else class="data-panel">
      <ResourceToolbar v-model="search" :loading="loading" :total="total" placeholder="搜索 Tool、调用 ID 或 Trace" @clear="search = ''" @refresh="load">
        <select v-model="selectedStatus" class="filter-select" aria-label="筛选 Tool 调用状态">
          <option v-for="status in statuses" :key="status.value" :value="status.value">{{ status.label }}</option>
        </select>
      </ResourceToolbar>
      <LoadingState v-if="loading && !calls.length" />
      <ErrorState v-else-if="error && !calls.length" :error="error" @retry="load" />
      <EmptyState v-else-if="!filteredCalls.length" title="没有匹配的 Tool 调用记录"><template #icon><Wrench :size="23" /></template></EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table tool-calls-table">
          <thead><tr><th>Tool</th><th>状态</th><th>调用方式</th><th>Trace</th><th>尝试次数</th><th>创建时间</th><th aria-label="操作" /></tr></thead>
          <tbody>
            <tr v-for="call in filteredCalls" :key="call.id">
              <td><div class="tool-call-name"><strong>{{ call.tool_name }}</strong><small>{{ call.external_tool_call_id }}</small></div></td>
              <td><StatusBadge :status="call.status" /></td>
              <td>{{ call.invocation_mode === "AUTONOMOUS" ? "自主调用" : "用户请求" }}</td>
              <td><code class="tool-call-id">{{ call.trace_id }}</code></td><td>{{ call.attempt_count }}</td><td>{{ formatDateTime(call.created_at) }}</td>
              <td><button class="icon-button" type="button" aria-label="查看调用详情" title="查看调用详情" @click="selectedCall = call"><Eye :size="16" /></button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <div v-if="selectedCall" class="operation-dialog-backdrop" @click.self="closeDetails">
      <section class="operation-dialog operation-dialog--wide" role="dialog" aria-modal="true" aria-label="Tool 调用详情">
        <header class="operation-dialog-header"><h3>Tool 调用详情</h3><button class="icon-button" type="button" aria-label="关闭详情" title="关闭详情" @click="closeDetails"><X :size="18" /></button></header>
        <div class="operation-dialog-body tool-call-details">
          <div class="tool-call-detail-summary"><span><small>Tool</small><strong>{{ selectedCall.tool_name }}</strong></span><span><small>状态</small><StatusBadge :status="selectedCall.status" /></span><span><small>创建时间</small><strong>{{ formatDateTime(selectedCall.created_at) }}</strong></span></div>
          <dl class="tool-call-meta"><div><dt>调用 ID</dt><dd><code>{{ selectedCall.external_tool_call_id }}</code></dd></div><div><dt>Trace ID</dt><dd><code>{{ selectedCall.trace_id }}</code></dd></div><div><dt>错误代码</dt><dd>{{ selectedCall.error_code || "-" }}</dd></div><div><dt>错误详情</dt><dd>{{ selectedCall.error_detail || "-" }}</dd></div></dl>
          <details class="raw-details" open><summary><Braces :size="14" />请求参数</summary><pre>{{ json(selectedCall.arguments) }}</pre></details>
          <details v-if="selectedCall.result" class="raw-details" open><summary><Braces :size="14" />执行结果</summary><pre>{{ json(selectedCall.result) }}</pre></details>
        </div>
      </section>
    </div>
  </div>
</template>
