<script setup lang="ts">
import {
  Bot,
  BrainCircuit,
  Cable,
  ChevronRight,
  CircleUserRound,
  LayoutDashboard,
  LogOut,
  Menu,
  MessagesSquare,
  Wrench,
  Package,
  Send,
  ShieldCheck,
  Smartphone,
  UserRoundCog,
  UsersRound,
  X,
} from "lucide-vue-next";
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { ApiError } from "@/api/client";
import { hasAllPermissions } from "@/auth/permissions";
import { authSession } from "@/auth/session";

const route = useRoute();
const router = useRouter();
const navigationOpen = ref(false);
const loggingOut = ref(false);
const logoutError = ref("");

const navigation = computed(() => {
  const groups = [
    {
      label: "工作台",
      items: [
        { label: "总览", to: "/", icon: LayoutDashboard },
        { label: "任务 Agent", to: "/task-agents", icon: BrainCircuit, permissions: ["agent.read"] },
        { label: "消息与 Trace", to: "/messages", icon: MessagesSquare, permissions: ["message.read"] },
        { label: "Tool 调用审计", to: "/tool-calls", icon: Wrench, permissions: ["audit.read"] },
        { label: "发送队列", to: "/outbox", icon: Send, permissions: ["outbox.read"] },
      ],
    },
    {
      label: "微信接入",
      items: [
        { label: "GeWe Connection", to: "/connections", icon: Cable, permissions: ["connection.read"] },
        { label: "微信账号", to: "/accounts", icon: Smartphone, permissions: ["account.read"] },
      ],
    },
    {
      label: "通讯录",
      items: [
        {
          label: "联系人",
          to: "/contacts",
          icon: UsersRound,
          permissions: ["directory.read", "account.read"],
        },
        {
          label: "已发现群",
          to: "/groups",
          icon: Bot,
          permissions: ["directory.read", "account.read"],
        },
      ],
    },
    {
      label: "扩展与权限",
      items: [
        { label: "插件", to: "/plugins", icon: Package, permissions: ["plugin.read"] },
        {
          label: "权限矩阵",
          to: "/permissions",
          icon: ShieldCheck,
          permissions: [
            "policy.read",
            "directory.read",
            "account.read",
            "connection.read",
            "plugin.read",
          ],
        },
        {
          label: "后台用户与角色",
          to: "/admin/users",
          icon: UserRoundCog,
          permissions: ["admin.user.manage"],
        },
      ],
    },
  ];
  return groups
    .map((group) => ({
      ...group,
      items: group.items.filter((item) =>
        hasAllPermissions(authSession.state.user, item.permissions || []),
      ),
    }))
    .filter((group) => group.items.length > 0);
});

const currentTitle = computed(() => String(route.meta.title || "管理后台"));
const currentUserLabel = computed(
  () => authSession.state.user?.display_name || authSession.state.user?.username || "管理员",
);

async function logout() {
  loggingOut.value = true;
  logoutError.value = "";
  try {
    await authSession.logout();
    await router.replace({ name: "login" });
  } catch (caught) {
    logoutError.value = caught instanceof ApiError ? caught.message : "退出登录失败";
  } finally {
    loggingOut.value = false;
  }
}

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
        <div class="topbar-actions">
          <span v-if="logoutError" class="topbar-error" role="alert">{{ logoutError }}</span>
          <span class="current-user">
            <CircleUserRound :size="18" stroke-width="1.8" />
            <span>
              <strong>{{ currentUserLabel }}</strong>
              <small>{{ authSession.state.user?.username }}</small>
            </span>
          </span>
          <button
            class="icon-button"
            type="button"
            :disabled="loggingOut"
            aria-label="退出登录"
            :title="loggingOut ? '正在退出' : '退出登录'"
            @click="logout"
          >
            <LogOut :size="18" />
          </button>
        </div>
      </header>

      <main class="page-content">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped>
.topbar-title {
  min-width: 0;
  flex: 1 1 auto;
}

.topbar-title h1 {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.topbar-actions {
  min-width: 0;
  flex: 0 1 auto;
}

.current-user {
  max-width: min(220px, 40vw);
}

.current-user > span {
  min-width: 0;
}

@media (max-width: 410px) {
  .topbar-title {
    gap: 6px;
  }

  .topbar-actions {
    gap: 5px;
  }

  .current-user {
    max-width: 34vw;
  }

  .current-user > svg {
    display: none;
  }
}
</style>
