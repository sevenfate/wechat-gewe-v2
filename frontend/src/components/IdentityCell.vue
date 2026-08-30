<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  name?: string | null;
  secondary?: string | null;
  avatarUrl?: string | null;
  square?: boolean;
}>();

const initial = computed(() => (props.name || props.secondary || "?").trim().slice(0, 1).toUpperCase());
</script>

<template>
  <div class="identity-cell">
    <img
      v-if="avatarUrl"
      class="identity-avatar"
      :class="{ 'identity-avatar--square': square }"
      :src="avatarUrl"
      alt=""
    />
    <span v-else class="identity-avatar identity-avatar--fallback" :class="{ 'identity-avatar--square': square }">
      {{ initial }}
    </span>
    <span class="identity-copy">
      <strong>{{ name || "未命名" }}</strong>
      <small v-if="secondary">{{ secondary }}</small>
    </span>
  </div>
</template>
