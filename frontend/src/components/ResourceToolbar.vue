<script setup lang="ts">
import { RefreshCw, Search, X } from "lucide-vue-next";

defineProps<{
  modelValue: string;
  loading?: boolean;
  placeholder?: string;
  total?: number | null;
}>();

const emit = defineEmits<{
  "update:modelValue": [value: string];
  search: [];
  clear: [];
  refresh: [];
}>();
</script>

<template>
  <div class="resource-toolbar">
    <form class="search-control" role="search" @submit.prevent="emit('search')">
      <Search :size="17" aria-hidden="true" />
      <input
        :value="modelValue"
        type="search"
        :placeholder="placeholder || '搜索'"
        aria-label="搜索"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      />
      <button
        v-if="modelValue"
        class="search-clear"
        type="button"
        aria-label="清除搜索"
        title="清除搜索"
        @click="emit('clear')"
      >
        <X :size="15" />
      </button>
    </form>

    <span v-if="total !== null && total !== undefined" class="resource-count">共 {{ total }} 项</span>

    <div class="resource-toolbar-actions">
      <slot />
      <button
        class="button button--secondary button--icon"
        type="button"
        :disabled="loading"
        aria-label="刷新"
        title="刷新"
        @click="emit('refresh')"
      >
        <RefreshCw :class="{ spin: loading }" :size="17" />
      </button>
    </div>
  </div>
</template>
