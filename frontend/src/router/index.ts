import { createRouter, createWebHistory } from "vue-router";

import { authSession } from "@/auth/session";

const routes = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/views/LoginView.vue"),
    meta: { title: "登录", layout: "auth", guestOnly: true },
  },
  {
    path: "/bootstrap",
    name: "bootstrap",
    component: () => import("@/views/BootstrapView.vue"),
    meta: { title: "首次初始化", layout: "auth", guestOnly: true },
  },
  {
    path: "/",
    name: "overview",
    component: () => import("@/views/OverviewView.vue"),
    meta: { title: "总览", requiresAuth: true },
  },
  {
    path: "/connections",
    name: "connections",
    component: () => import("@/views/ConnectionsView.vue"),
    meta: { title: "GeWe Connection", requiresAuth: true },
  },
  {
    path: "/accounts",
    name: "accounts",
    component: () => import("@/views/AccountsView.vue"),
    meta: { title: "微信账号", requiresAuth: true },
  },
  {
    path: "/contacts",
    name: "contacts",
    component: () => import("@/views/ContactsView.vue"),
    meta: { title: "联系人", requiresAuth: true },
  },
  {
    path: "/groups",
    name: "groups",
    component: () => import("@/views/GroupsView.vue"),
    meta: { title: "已发现群", requiresAuth: true },
  },
  {
    path: "/plugins",
    name: "plugins",
    component: () => import("@/views/PluginsView.vue"),
    meta: { title: "插件", requiresAuth: true },
  },
  {
    path: "/permissions",
    name: "permissions",
    component: () => import("@/views/PermissionsView.vue"),
    meta: { title: "权限矩阵", requiresAuth: true },
  },
  {
    path: "/outbox",
    name: "outbox",
    component: () => import("@/views/OutboxView.vue"),
    meta: { title: "发送队列", requiresAuth: true },
  },
  {
    path: "/messages",
    name: "messages",
    component: () => import("@/views/MessagesView.vue"),
    meta: { title: "消息与 Trace", requiresAuth: true },
  },
  {
    path: "/task-agents",
    name: "task-agents",
    component: () => import("@/views/TaskAgentView.vue"),
    meta: { title: "任务 Agent", requiresAuth: true },
  },
  {
    path: "/admin/users",
    name: "admin-rbac",
    component: () => import("@/views/AdminRbacView.vue"),
    meta: { title: "后台用户与角色", requiresAuth: true },
  },
  {
    path: "/:pathMatch(.*)*",
    redirect: "/",
  },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  await authSession.restore();

  if (to.meta.requiresAuth && !authSession.state.user) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.meta.guestOnly && authSession.state.user) {
    return { name: "overview" };
  }
  return true;
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "管理后台")} - 微信机器人管理平台`;
});
