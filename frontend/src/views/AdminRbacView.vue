<script setup lang="ts">
import { KeyRound, Plus, Shield, UserRoundCog, UsersRound, X } from "lucide-vue-next";
import { computed, onMounted, reactive, ref } from "vue";

import { ApiError } from "@/api/client";
import {
  adminRbacApi,
  type AdminUser,
  type RbacPermission,
  type RbacRole,
} from "@/api/operations";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";

type DialogMode = "create-user" | "user-roles" | "create-role" | "role-permissions";

const users = ref<AdminUser[]>([]);
const roles = ref<RbacRole[]>([]);
const permissions = ref<RbacPermission[]>([]);
const loading = ref(false);
const error = ref<ApiError | null>(null);
const actionError = ref<ApiError | null>(null);
const submitting = ref(false);
const dialog = ref<DialogMode | null>(null);
const selectedUser = ref<AdminUser | null>(null);
const selectedRole = ref<RbacRole | null>(null);
const selectedCodes = ref<string[]>([]);
const userDraft = reactive({ username: "", display_name: "", password: "" });
const roleDraft = reactive({ code: "", name: "" });

const isOwner = computed(() => authSession.state.user?.roles.includes("owner") === true);

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const [userResult, roleResult, permissionResult] = await Promise.all([
      adminRbacApi.users.list(),
      adminRbacApi.roles.list(),
      adminRbacApi.permissions.list(),
    ]);
    users.value = userResult.items;
    roles.value = roleResult.items;
    permissions.value = permissionResult.items;
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : new ApiError("读取后台权限失败", 0);
  } finally {
    loading.value = false;
  }
}

function openDialog(mode: DialogMode, target?: AdminUser | RbacRole) {
  dialog.value = mode;
  actionError.value = null;
  selectedUser.value = mode === "user-roles" ? (target as AdminUser) : null;
  selectedRole.value = mode === "role-permissions" ? (target as RbacRole) : null;
  selectedCodes.value =
    mode === "user-roles"
      ? [...((target as AdminUser).roles || [])]
      : mode === "role-permissions"
        ? [...((target as RbacRole).permissions || [])]
        : [];
  Object.assign(userDraft, { username: "", display_name: "", password: "" });
  Object.assign(roleDraft, { code: "", name: "" });
}

function closeDialog() {
  if (submitting.value) return;
  dialog.value = null;
  selectedUser.value = null;
  selectedRole.value = null;
  selectedCodes.value = [];
  actionError.value = null;
}

function toggleCode(code: string, checked: boolean) {
  selectedCodes.value = checked
    ? [...new Set([...selectedCodes.value, code])]
    : selectedCodes.value.filter((item) => item !== code);
}

async function submitDialog() {
  const mode = dialog.value;
  if (!mode) return;
  submitting.value = true;
  actionError.value = null;
  try {
    if (mode === "create-user") {
      await adminRbacApi.users.create({
        username: userDraft.username.trim(),
        display_name: userDraft.display_name.trim() || undefined,
        password: userDraft.password,
      });
    } else if (mode === "user-roles" && selectedUser.value) {
      await adminRbacApi.users.setRoles(selectedUser.value.id, selectedCodes.value);
    } else if (mode === "create-role") {
      await adminRbacApi.roles.create({ code: roleDraft.code.trim(), name: roleDraft.name.trim() });
    } else if (mode === "role-permissions" && selectedRole.value) {
      await adminRbacApi.roles.setPermissions(selectedRole.value.id, selectedCodes.value);
    }
    submitting.value = false;
    closeDialog();
    await load();
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught : new ApiError("保存失败", 0);
  } finally {
    submitting.value = false;
  }
}

async function toggleUserStatus(user: AdminUser) {
  const next = user.status === "ACTIVE" ? "DISABLED" : "ACTIVE";
  if (!window.confirm(`${next === "DISABLED" ? "停用" : "启用"}后台用户 ${user.username}？`)) return;
  actionError.value = null;
  try {
    await adminRbacApi.users.setStatus(user.id, next);
    await load();
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught : new ApiError("修改用户状态失败", 0);
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page-stack">
    <PageHeader title="后台用户与角色" description="管理后台登录身份、角色和管理权限" />

    <ErrorState v-if="error" :error="error" @retry="load" />
    <LoadingState v-else-if="loading && !users.length && !roles.length" />
    <div v-else class="rbac-grid">
      <section class="rbac-section">
        <header class="rbac-section-header">
          <h3>后台用户</h3>
          <button class="button button--primary" type="button" @click="openDialog('create-user')">
            <Plus :size="15" />新建用户
          </button>
        </header>
        <EmptyState v-if="!users.length" title="暂无后台用户">
          <template #icon><UsersRound :size="23" /></template>
        </EmptyState>
        <div v-else class="rbac-item-list">
          <article v-for="user in users" :key="user.id" class="rbac-item">
            <div class="rbac-item-copy">
              <strong>{{ user.display_name || user.username }}</strong>
              <small>{{ user.username }} · auth v{{ user.auth_version }}</small>
              <div class="code-chip-list">
                <span v-for="code in user.roles" :key="code" class="code-chip">{{ code }}</span>
                <span v-if="!user.roles.length" class="muted-text">未分配角色</span>
              </div>
            </div>
            <div class="row-actions">
              <StatusBadge :status="user.status" />
              <button
                v-if="isOwner"
                class="button button--secondary"
                type="button"
                @click="openDialog('user-roles', user)"
              >
                <UserRoundCog :size="15" />角色
              </button>
              <button
                class="button"
                :class="user.status === 'ACTIVE' ? 'button--danger' : 'button--secondary'"
                type="button"
                @click="toggleUserStatus(user)"
              >
                {{ user.status === "ACTIVE" ? "停用" : "启用" }}
              </button>
            </div>
          </article>
        </div>
      </section>

      <section class="rbac-section">
        <header class="rbac-section-header">
          <h3>角色</h3>
          <button
            v-if="isOwner"
            class="button button--primary"
            type="button"
            @click="openDialog('create-role')"
          >
            <Plus :size="15" />新建角色
          </button>
        </header>
        <EmptyState v-if="!roles.length" title="暂无角色">
          <template #icon><Shield :size="23" /></template>
        </EmptyState>
        <div v-else class="rbac-item-list">
          <article v-for="role in roles" :key="role.id" class="rbac-item">
            <div class="rbac-item-copy">
              <strong>{{ role.name }}</strong>
              <small>{{ role.code }}{{ role.is_system ? " · 系统角色" : "" }}</small>
              <div class="code-chip-list">
                <span v-for="code in role.permissions.slice(0, 5)" :key="code" class="code-chip">{{ code }}</span>
                <span v-if="role.permissions.length > 5" class="code-chip">+{{ role.permissions.length - 5 }}</span>
              </div>
            </div>
            <div class="row-actions">
              <StatusBadge :status="role.active ? 'ACTIVE' : 'DISABLED'" />
              <button
                v-if="isOwner && !role.is_system"
                class="button button--secondary"
                type="button"
                @click="openDialog('role-permissions', role)"
              >
                <KeyRound :size="15" />权限
              </button>
            </div>
          </article>
        </div>
      </section>
    </div>

    <div v-if="actionError && !dialog" class="inline-error" role="alert">{{ actionError.message }}</div>

    <div v-if="dialog" class="operation-dialog-backdrop" @click.self="closeDialog">
      <form
        class="operation-dialog"
        :class="{ 'operation-dialog--wide': dialog === 'role-permissions' }"
        role="dialog"
        aria-modal="true"
        @submit.prevent="submitDialog"
      >
        <header class="operation-dialog-header">
          <h3>
            {{
              dialog === "create-user"
                ? "新建后台用户"
                : dialog === "user-roles"
                  ? `分配角色：${selectedUser?.username}`
                  : dialog === "create-role"
                    ? "新建角色"
                    : `分配权限：${selectedRole?.name}`
            }}
          </h3>
          <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeDialog">
            <X :size="18" />
          </button>
        </header>

        <div class="operation-dialog-body">
          <template v-if="dialog === 'create-user'">
            <label class="field-control">
              <span>用户名</span>
              <input v-model="userDraft.username" type="text" required minlength="3" maxlength="80" />
            </label>
            <label class="field-control">
              <span>显示名称</span>
              <input v-model="userDraft.display_name" type="text" maxlength="120" />
            </label>
            <label class="field-control">
              <span>初始密码</span>
              <input v-model="userDraft.password" type="password" required minlength="12" autocomplete="new-password" />
            </label>
          </template>

          <template v-else-if="dialog === 'create-role'">
            <label class="field-control">
              <span>角色代码</span>
              <input
                v-model="roleDraft.code"
                type="text"
                required
                minlength="2"
                maxlength="120"
                pattern="[a-z](?:[a-z0-9_]|-)*"
              />
            </label>
            <label class="field-control">
              <span>角色名称</span>
              <input v-model="roleDraft.name" type="text" required maxlength="120" />
            </label>
          </template>

          <div v-else-if="dialog === 'user-roles'" class="check-grid">
            <label v-for="role in roles" :key="role.id" class="check-option">
              <input
                type="checkbox"
                :checked="selectedCodes.includes(role.code)"
                @change="toggleCode(role.code, ($event.target as HTMLInputElement).checked)"
              />
              <span class="check-option-copy">
                <strong>{{ role.name }}</strong>
                <small>{{ role.code }}</small>
              </span>
            </label>
          </div>

          <div v-else class="check-grid">
            <label v-for="permission in permissions" :key="permission.id" class="check-option">
              <input
                type="checkbox"
                :checked="selectedCodes.includes(permission.code)"
                @change="toggleCode(permission.code, ($event.target as HTMLInputElement).checked)"
              />
              <span class="check-option-copy">
                <strong>{{ permission.code }}</strong>
                <small>{{ permission.description || "-" }}</small>
              </span>
            </label>
          </div>

          <div v-if="actionError" class="inline-error" role="alert">{{ actionError.message }}</div>
        </div>

        <footer class="operation-dialog-actions">
          <button class="button button--secondary" type="button" :disabled="submitting" @click="closeDialog">
            取消
          </button>
          <button class="button button--primary" type="submit" :disabled="submitting">
            {{ submitting ? "正在保存" : "保存" }}
          </button>
        </footer>
      </form>
    </div>
  </div>
</template>
