import { createRouter, createWebHistory } from "vue-router";

const routes = [
  {
    path: "/",
    name: "overview",
    component: () => import("@/views/OverviewView.vue"),
    meta: { title: "总览" },
  },
  {
    path: "/connections",
    name: "connections",
    component: () => import("@/views/ConnectionsView.vue"),
    meta: { title: "GeWe Connection" },
  },
  {
    path: "/accounts",
    name: "accounts",
    component: () => import("@/views/AccountsView.vue"),
    meta: { title: "微信账号" },
  },
  {
    path: "/contacts",
    name: "contacts",
    component: () => import("@/views/ContactsView.vue"),
    meta: { title: "联系人" },
  },
  {
    path: "/groups",
    name: "groups",
    component: () => import("@/views/GroupsView.vue"),
    meta: { title: "已发现群" },
  },
  {
    path: "/plugins",
    name: "plugins",
    component: () => import("@/views/PluginsView.vue"),
    meta: { title: "插件" },
  },
  {
    path: "/permissions",
    name: "permissions",
    component: () => import("@/views/PermissionsView.vue"),
    meta: { title: "权限矩阵" },
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

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "管理后台")} - 微信机器人管理平台`;
});
