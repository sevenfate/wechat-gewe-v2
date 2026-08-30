<script setup lang="ts">
import {
  Bot,
  Cable,
  ChevronRight,
  LayoutDashboard,
  Menu,
  Package,
  ShieldCheck,
  Smartphone,
  UsersRound,
  X,
} from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();
const navigationOpen = ref(false);

const navigation = [
  {
    label: "工作台",
    items: [{ label: "总览", to: "/", icon: LayoutDashboard }],
  },
  {
    label: "微信接入",
    items: [
      { label: "GeWe Connection", to: "/connections", icon: Cable },
      { label: "微信账号", to: "/accounts", icon: Smartphone },
    ],
  },
  {
    label: "通讯录",
    items: [
      { label: "联系人", to: "/contacts", icon: UsersRound },
      { label: "已发现群", to: "/groups", icon: Bot },
    ],
  },
  {
    label: "扩展与权限",
    items: [
      { label: "插件", to: "/plugins", icon: Package },
      { label: "权限矩阵", to: "/permissions", icon: ShieldCheck },
    ],
  },
];

const currentTitle = computed(() => String(route.meta.title || "管理后台"));

watch(
  () => route.fullPath,
  () => {
    navigationOpen.value = false;
  },
);
</script>

<template>
  <div class="app-shell">
    <button
      v-if="navigationOpen"
      class="navigation-overlay"
      type="button"
      aria-label="关闭导航"
      @click="navigationOpen = false"
    />

    <aside class="sidebar" :class="{ 'sidebar--open': navigationOpen }">
      <div class="sidebar-brand">
        <span class="brand-mark"><Bot :size="21" stroke-width="1.8" /></span>
        <span class="brand-copy">
          <strong>微信机器人</strong>
          <small>管理平台</small>
        </span>
        <button
          class="sidebar-close icon-button"
          type="button"
          aria-label="关闭导航"
          title="关闭导航"
          @click="navigationOpen = false"
        >
          <X :size="18" />
        </button>
      </div>

      <nav class="sidebar-nav" aria-label="主导航">
        <section v-for="group in navigation" :key="group.label" class="nav-group">
          <p class="nav-group-label">{{ group.label }}</p>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" class="nav-link">
            <component :is="item.icon" :size="18" stroke-width="1.8" />
            <span>{{ item.label }}</span>
            <ChevronRight class="nav-link-arrow" :size="15" />
          </RouterLink>
        </section>
      </nav>

      <div class="sidebar-footer">
        <span class="status-dot status-dot--neutral" />
        <span>API</span>
        <code>/api/v1</code>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div class="topbar-title">
          <button
            class="mobile-menu icon-button"
            type="button"
            aria-label="打开导航"
            title="打开导航"
            @click="navigationOpen = true"
          >
            <Menu :size="20" />
          </button>
          <h1>{{ currentTitle }}</h1>
        </div>
      </header>

      <main class="page-content">
        <RouterView />
      </main>
    </div>
  </div>
</template>
