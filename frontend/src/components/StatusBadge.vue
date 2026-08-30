<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  status?: string | null;
  fallback?: string;
}>();

const normalized = computed(() => (props.status || "UNKNOWN").toUpperCase());
const tone = computed(() => {
  if (["ONLINE", "HEALTHY", "ACTIVE", "READY", "VERIFIED", "AVAILABLE", "ENABLED", "RUNNING", "SYNCED", "SENT", "DISPATCHED", "SUCCESS", "COMPLETED", "ANSWERED"].includes(normalized.value)) {
    return "success";
  }
  if (["OFFLINE", "FAILED", "FAILED_FINAL", "ERROR", "UNHEALTHY", "DENY", "DENIED", "DISABLED", "INACTIVE", "QUARANTINED", "REJECTED", "RETIRED", "CANCELLED", "EXPIRED"].includes(normalized.value)) {
    return "danger";
  }
  if (["ASK", "PENDING", "CLAIMED", "SENDING", "FAILED_RETRYABLE", "RECEIVED", "NORMALIZED", "DISPATCHING", "PLACEHOLDER", "QR_PENDING", "SCANNED", "STARTING", "DRAINING", "RECONNECTING", "DEGRADED", "SYNCING", "QUEUED", "WAITING_APPROVAL", "WAITING_USER"].includes(normalized.value)) {
    return "warning";
  }
  return "neutral";
});

const translatedLabels: Record<string, string> = {
  ACTIVE: "启用",
  AVAILABLE: "可用",
  ASK: "需确认",
  CANCELLED: "已取消",
  CLAIMED: "已领取",
  DEGRADED: "降级",
  DENIED: "已拒绝",
  DISABLED: "停用",
  DRAINING: "停止中",
  DISPATCHED: "已分发",
  DISPATCHING: "分发中",
  ENABLED: "启用",
  COMPLETED: "已完成",
  ERROR: "错误",
  FAILED: "失败",
  FAILED_FINAL: "最终失败",
  FAILED_RETRYABLE: "等待重试",
  HEALTHY: "健康",
  INACTIVE: "未启用",
  IGNORED_SELF: "忽略自发",
  NEED_QR: "需扫码",
  OFFLINE: "离线",
  ONLINE: "在线",
  NORMALIZED: "已标准化",
  PENDING: "等待中",
  PAUSED: "已暂停",
  PLACEHOLDER: "待补全",
  QR_PENDING: "等待扫码",
  READY: "就绪",
  RECEIVED: "已接收",
  REJECTED: "已拒绝",
  RETIRED: "已退役",
  RECONNECTING: "重连中",
  SCANNED: "已扫码",
  SENDING: "发送中",
  SENT: "已发送",
  STARTING: "启动中",
  STOPPED: "已停止",
  SUCCESS: "成功",
  SYNCED: "已同步",
  SYNCING: "同步中",
  UNBOUND: "未绑定",
  UNHEALTHY: "异常",
  UNKNOWN: "未知",
  VERIFIED: "已验证",
  RUNNING: "运行中",
  QUEUED: "排队中",
  WAITING_APPROVAL: "等待审批",
  WAITING_USER: "等待用户",
  ANSWERED: "已回答",
  EXPIRED: "已过期",
  QUARANTINED: "已隔离",
};

const label = computed(() => {
  if (!props.status) return props.fallback || "未知";
  return translatedLabels[normalized.value] || props.status;
});
</script>

<template>
  <span class="status-badge" :class="`status-badge--${tone}`">
    <span class="status-badge-dot" />
    {{ label }}
  </span>
</template>
