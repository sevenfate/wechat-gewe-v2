<script setup lang="ts">
import { Bot } from "lucide-vue-next";
import { computed } from "vue";
import { useRoute } from "vue-router";

import { authSession } from "@/auth/session";
import AppShell from "@/components/AppShell.vue";

const route = useRoute();
const authLayout = computed(() => route.meta.layout === "auth");
</script>

<template>
  <div v-if="!authSession.state.restored" class="app-loading" aria-live="polite">
    <span class="brand-mark"><Bot :size="21" stroke-width="1.8" /></span>
    <span>正在恢复会话</span>
  </div>
  <RouterView v-else-if="authLayout" />
  <AppShell v-else />
</template>
