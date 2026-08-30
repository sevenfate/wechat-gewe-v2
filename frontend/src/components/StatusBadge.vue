<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  status?: string | null;
  fallback?: string;
}>();

const normalized = computed(() => (props.status || "UNKNOWN").toUpperCase());
const tone = computed(() => {
  if (["ONLINE", "HEALTHY", "ACTIVE", "READY", "VERIFIED", "ENABLED", "SYNCED"].includes(normalized.value)) {
    return "success";
  }
  if (["OFFLINE", "FAILED", "ERROR", "UNHEALTHY", "DENY", "DISABLED"].includes(normalized.value)) {
    return "danger";
  }
  if (["PENDING", "QR_PENDING", "SCANNED", "RECONNECTING", "DEGRADED", "SYNCING"].includes(normalized.value)) {
    return "warning";
  }
  return "neutral";
});

const translatedLabels: Record<string, string> = {
  ACTIVE: "启用",
  DEGRADED: "降级",
  DISABLED: "停用",
  ENABLED: "启用",
  ERROR: "错误",
  FAILED: "失败",
  HEALTHY: "健康",
  NEED_QR: "需扫码",
  OFFLINE: "离线",
  ONLINE: "在线",
  PENDING: "等待中",
  QR_PENDING: "等待扫码",
  READY: "就绪",
  RECONNECTING: "重连中",
  SCANNED: "已扫码",
  SYNCED: "已同步",
  SYNCING: "同步中",
  UNBOUND: "未绑定",
  UNHEALTHY: "异常",
  UNKNOWN: "未知",
  VERIFIED: "已验证",
};

const label = computed(() => translatedLabels[normalized.value] || props.status || props.fallback || "未知");
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`">
    <span class="status-badge-dot" />
    {{ label }}
  </span>
</template>
