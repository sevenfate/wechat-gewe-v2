<script setup lang="ts">
import { Inbox } from "lucide-vue-next";

defineProps<{
  title: string;
  detail?: string;
}>();
</script>

<template>
  <div class="empty-state">
    <span class="empty-state-icon">
      <slot name="icon"><Inbox :size="23" /></slot>
    </span>
    <h3>{{ title }}</h3>
    <p v-if="detail">{{ detail }}</p>
    <div v-if="$slots.action" class="empty-state-action"><slot name="action" /></div>
  </div>
</template>
