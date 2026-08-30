<script setup lang="ts">
import { CircleAlert, RefreshCw } from "lucide-vue-next";

import type { ApiError } from "@/api/client";

defineProps<{ error: ApiError }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div class="error-state" role="alert">
    <CircleAlert :size="20" />
    <div class="error-state-copy">
      <strong>{{ error.status === 0 ? "管理 API 不可达" : "数据加载失败" }}</strong>
      <span>{{ error.message }}</span>
      <code v-if="error.traceId">Trace {{ error.traceId }}</code>
    </div>
    <button class="button button--secondary" type="button" @click="$emit('retry')">
      <RefreshCw :size="15" />
      重试
    </button>
  </div>
</template>
