<script setup lang="ts">
import {
  Check,
  CloudUpload,
  Copy,
  KeyRound,
  Plus,
  Power,
  Server,
  X,
} from "lucide-vue-next";
import { computed, reactive, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";
import { managementApi } from "@/api/resources";
import type { CreateConnectionInput, GeweConnection } from "@/api/types";
import {
  wechatOperationsApi,
  type CallbackManagementMode,
  type WechatConnection,
} from "@/api/wechat-operations";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime } from "@/utils/format";
import "@/styles/wechat-operations.css";

type ConnectionDialog = "token" | "apply";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<GeweConnection>(
  managementApi.connections.list,
);

const showForm = ref(false);
const submitting = ref(false);
const formError = shallowRef<ApiError | null>(null);
const actionError = shallowRef<ApiError | null>(null);
const feedback = ref("");
const copiedId = ref<string | null>(null);
const createdId = ref<string | null>(null);
const activeAction = ref("");
const dialog = ref<ConnectionDialog | null>(null);
const selectedConnection = ref<GeweConnection | null>(null);
const tokenDraft = ref("");
const canWrite = computed(() =>
  authSession.state.user?.roles.includes("owner") === true ||
  authSession.state.user?.permissions.includes("connection.write") === true,
);

const initialDraft: CreateConnectionInput = {
  name: "",
  api_base_url: "",
  token: "",
};
const draft = reactive<CreateConnectionInput>({ ...initialDraft });

function asApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError(fallback, 0, "UNKNOWN_ERROR");
}

function resetForm() {
  Object.assign(draft, initialDraft);
  formError.value = null;
}

function toggleForm() {
  if (!canWrite.value) return;
  if (showForm.value) {
    closeForm();
  } else {
    showForm.value = true;
  }
}

function closeForm() {
  if (submitting.value) return;
  showForm.value = false;
  resetForm();
}

function replaceConnection(updated: WechatConnection) {
  items.value = items.value.map((item) => (item.id === updated.id ? updated : item));
}

function isConnectionBusy(connectionId: string): boolean {
  return activeAction.value.startsWith(`${connectionId}:`);
}

async function runAction(
  connection: GeweConnection,
  action: string,
  fallback: string,
  success: string,
  task: () => Promise<WechatConnection>,
): Promise<boolean> {
  activeAction.value = `${connection.id}:${action}`;
  actionError.value = null;
  feedback.value = "";
  try {
    replaceConnection(await task());
    feedback.value = success;
    return true;
  } catch (caught) {
    actionError.value = asApiError(caught, fallback);
    return false;
  } finally {
    activeAction.value = "";
  }
}

async function createConnection() {
  if (!canWrite.value) return;
  submitting.value = true;
  formError.value = null;
  feedback.value = "";
  try {
    const connection = await managementApi.connections.create({
      name: draft.name.trim(),
      api_base_url: draft.api_base_url.trim(),
      token: draft.token,
    });
    createdId.value = connection.id;
    draft.token = "";
    showForm.value = false;
    await reload();
    resetForm();
    feedback.value = `连接“${connection.name}”已创建，Token 仅保留指纹。`;
  } catch (caught) {
    formError.value = asApiError(caught, "创建连接时发生未知错误");
  } finally {
    submitting.value = false;
  }
}

async function copyCallback(connection: GeweConnection) {
  if (!connection.callback_url) return;
  actionError.value = null;
  try {
    await navigator.clipboard.writeText(connection.callback_url);
    copiedId.value = connection.id;
    window.setTimeout(() => {
      if (copiedId.value === connection.id) copiedId.value = null;
    }, 1800);
  } catch (caught) {
    actionError.value = asApiError(caught, "浏览器未能复制回调地址");
  }
}

async function changeCallbackMode(connection: GeweConnection, event: Event) {
  const select = event.currentTarget as HTMLSelectElement;
  const nextMode = select.value as CallbackManagementMode;
  if (!canWrite.value) {
    select.value = connection.callback_mode;
    return;
  }
  if (nextMode === connection.callback_mode) return;
  const changed = await runAction(
    connection,
    "mode",
    "切换回调管理方式失败",
    nextMode === "MANUAL"
      ? `“${connection.name}”已切换为手动管理，平台不会自动下发回调。`
      : `“${connection.name}”已切换为平台代管；尚未下发，请按需点击“应用回调”。`,
    () => wechatOperationsApi.connections.setCallbackMode(connection.id, nextMode),
  );
  if (!changed) select.value = connection.callback_mode;
}

async function toggleStatus(connection: GeweConnection) {
  if (!canWrite.value) return;
  const nextStatus = connection.status === "DISABLED" ? "ACTIVE" : "DISABLED";
  await runAction(
    connection,
    "status",
    "修改连接状态失败",
    `“${connection.name}”已${nextStatus === "ACTIVE" ? "启用" : "停用"}。`,
    () => wechatOperationsApi.connections.setStatus(connection.id, nextStatus),
  );
}

function openDialog(next: ConnectionDialog, connection: GeweConnection) {
  if (!canWrite.value) return;
  dialog.value = next;
  selectedConnection.value = connection;
  tokenDraft.value = "";
  actionError.value = null;
}

function closeDialog() {
  if (activeAction.value) return;
  dialog.value = null;
  selectedConnection.value = null;
  tokenDraft.value = "";
  actionError.value = null;
}

async function rotateToken() {
  if (!canWrite.value) return;
  const connection = selectedConnection.value;
  if (!connection || !tokenDraft.value) return;
  const succeeded = await runAction(
    connection,
    "token",
    "更换 Token 失败",
    `“${connection.name}”的 Token 已更换。`,
    () => wechatOperationsApi.connections.rotateToken(connection.id, tokenDraft.value),
  );
  if (succeeded) closeDialog();
}

async function applyManagedCallback() {
  if (!canWrite.value) return;
  const connection = selectedConnection.value;
  if (!connection) return;
  activeAction.value = `${connection.id}:apply`;
  actionError.value = null;
  feedback.value = "";
  try {
    const result = await wechatOperationsApi.connections.applyManagedCallback(connection.id);
    replaceConnection(result.connection);
    feedback.value = result.applied
      ? `“${connection.name}”的回调地址已下发到 GeWe。`
      : `“${connection.name}”的回调地址未下发。`;
    activeAction.value = "";
    closeDialog();
  } catch (caught) {
    actionError.value = asApiError(caught, "应用代管回调失败");
  } finally {
    activeAction.value = "";
  }
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="GeWe Connection" description="管理 API 地址、凭据指纹与独立回调入口">
      <template #actions>
        <button class="button button--primary" type="button" :disabled="!canWrite" @click="toggleForm">
          <X v-if="showForm" :size="16" />
          <Plus v-else :size="16" />
          {{ showForm ? "收起" : "新建连接" }}
        </button>
      </template>
    </PageHeader>

    <div v-if="feedback" class="wechat-feedback wechat-feedback--success" role="status">
      <Check :size="17" />
      <span>{{ feedback }}</span>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭提示" @click="feedback = ''">
        <X :size="15" />
      </button>
    </div>
    <div v-if="actionError && !dialog" class="wechat-feedback wechat-feedback--error" role="alert">
      <span>{{ actionError.message }}</span>
      <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭错误" @click="actionError = null">
        <X :size="15" />
      </button>
    </div>

    <section v-if="showForm" class="form-panel" aria-labelledby="connection-form-title">
      <div class="form-panel-header">
        <div>
          <h3 id="connection-form-title">新建 GeWe Connection</h3>
          <p>Token 保存后仅显示不可逆指纹</p>
        </div>
        <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeForm">
          <X :size="18" />
        </button>
      </div>

      <form class="connection-form" @submit.prevent="createConnection">
        <label class="field-control">
          <span>连接名称</span>
          <input
            v-model="draft.name"
            type="text"
            required
            maxlength="120"
            autocomplete="off"
            placeholder="例如：主连接"
          />
        </label>
        <label class="field-control">
          <span>GeWe API 地址</span>
          <input
            v-model="draft.api_base_url"
            type="url"
            required
            autocomplete="url"
            placeholder="https://..."
            inputmode="url"
          />
        </label>
        <label class="field-control field-control--secret">
          <span>GeWe Token</span>
          <span class="secret-input">
            <KeyRound :size="16" />
            <input v-model="draft.token" type="password" required autocomplete="new-password" />
          </span>
        </label>

        <div v-if="formError" class="inline-error" role="alert">
          {{ formError.message }}
          <code v-if="formError.traceId">Trace {{ formError.traceId }}</code>
        </div>

        <div class="form-actions field-control--wide">
          <button class="button button--secondary" type="button" :disabled="submitting" @click="closeForm">
            取消
          </button>
          <button class="button button--primary" type="submit" :disabled="submitting">
            {{ submitting ? "正在创建" : "创建连接" }}
          </button>
        </div>
      </form>
    </section>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索连接名称或 API 地址"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState
        v-else-if="!items.length"
        :title="search ? '没有匹配的连接' : '尚未创建 GeWe Connection'"
        :detail="search ? undefined : '创建后可查看平台生成的回调地址'"
      >
        <template v-if="!search" #action>
          <button class="button button--primary" type="button" :disabled="!canWrite" @click="showForm = true">
            <Plus :size="16" />
            新建连接
          </button>
        </template>
      </EmptyState>

      <div v-else class="table-scroll">
        <table class="data-table wechat-connection-table">
          <thead>
            <tr>
              <th>连接</th>
              <th>状态</th>
              <th>Token 指纹</th>
              <th>回调地址</th>
              <th>管理方式</th>
              <th>最近回调</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="connection in items"
              :key="connection.id"
              :class="{ 'row-created': createdId === connection.id }"
            >
              <td>
                <div class="primary-cell">
                  <span class="resource-icon"><Server :size="17" /></span>
                  <span>
                    <strong>{{ connection.name }}</strong>
                    <small>{{ connection.api_base_url }}</small>
                  </span>
                </div>
              </td>
              <td><StatusBadge :status="connection.status" /></td>
              <td><code>{{ connection.token_fingerprint || "-" }}</code></td>
              <td>
                <div class="callback-cell">
                  <span>
                    <code :title="connection.callback_url">{{ connection.callback_url }}</code>
                    <small>
                      {{
                        connection.callback_expected_url === connection.callback_url
                          ? "平台记录：已下发"
                          : "平台记录：尚未下发"
                      }}
                    </small>
                  </span>
                  <button
                    class="icon-button icon-button--small"
                    type="button"
                    aria-label="复制回调地址"
                    :title="copiedId === connection.id ? '已复制' : '复制回调地址'"
                    @click="copyCallback(connection)"
                  >
                    <Check v-if="copiedId === connection.id" :size="15" />
                    <Copy v-else :size="15" />
                  </button>
                </div>
              </td>
              <td>
                <select
                  class="wechat-compact-select"
                  :value="connection.callback_mode"
                  :disabled="!canWrite || isConnectionBusy(connection.id)"
                  :aria-label="`设置 ${connection.name} 的回调管理方式`"
                  @change="changeCallbackMode(connection, $event)"
                >
                  <option value="MANUAL">手动管理</option>
                  <option value="PLATFORM_MANAGED">平台代管</option>
                </select>
              </td>
              <td>
                <span class="stacked-cell">
                  <span>{{ formatDateTime(connection.last_callback_at) }}</span>
                  <small v-if="connection.last_callback_error" :title="connection.last_callback_error">
                    {{ connection.last_callback_error }}
                  </small>
                  <small v-else-if="connection.callback_verified_at" class="wechat-muted-detail">
                    已验证 {{ formatDateTime(connection.callback_verified_at) }}
                  </small>
                </span>
              </td>
              <td>{{ formatDateTime(connection.updated_at) }}</td>
              <td>
                <div class="wechat-row-actions">
                  <button
                    class="icon-button"
                    type="button"
                    :disabled="!canWrite || isConnectionBusy(connection.id)"
                    aria-label="更换 Token"
                    title="更换 Token"
                    @click="openDialog('token', connection)"
                  >
                    <KeyRound :size="16" />
                  </button>
                  <button
                    class="button button--secondary"
                    type="button"
                    :disabled="!canWrite || connection.callback_mode !== 'PLATFORM_MANAGED' || isConnectionBusy(connection.id)"
                    :title="connection.callback_mode === 'PLATFORM_MANAGED' ? '向 GeWe 下发当前回调地址' : '先切换为平台代管'"
                    @click="openDialog('apply', connection)"
                  >
                    <CloudUpload :size="15" />
                    应用回调
                  </button>
                  <button
                    class="button"
                    :class="connection.status === 'DISABLED' ? 'button--secondary' : 'button--danger'"
                    type="button"
                    :disabled="!canWrite || isConnectionBusy(connection.id)"
                    @click="toggleStatus(connection)"
                  >
                    <Power :size="15" />
                    {{ connection.status === "DISABLED" ? "启用" : "停用" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <Teleport to="body">
      <div v-if="dialog && selectedConnection" class="wechat-dialog-backdrop" @click.self="closeDialog">
        <form
          class="wechat-dialog"
          role="dialog"
          aria-modal="true"
          :aria-labelledby="`connection-${dialog}-title`"
          @submit.prevent="dialog === 'token' ? rotateToken() : applyManagedCallback()"
        >
          <header class="wechat-dialog-header">
            <div>
              <h3 :id="`connection-${dialog}-title`">
                {{ dialog === "token" ? "更换 GeWe Token" : "确认应用代管回调" }}
              </h3>
              <p>{{ selectedConnection.name }}</p>
            </div>
            <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeDialog">
              <X :size="18" />
            </button>
          </header>

          <div class="wechat-dialog-body">
            <label v-if="dialog === 'token'" class="field-control">
              <span>新 Token</span>
              <span class="secret-input">
                <KeyRound :size="16" />
                <input
                  v-model="tokenDraft"
                  type="password"
                  required
                  autocomplete="new-password"
                  autofocus
                />
              </span>
              <small>原 Token 不会回显；提交成功后输入值会立即清空。</small>
            </label>
            <div v-else class="wechat-confirm-copy">
              <p>此操作会立即调用 GeWe，将下面的地址设置为该连接的回调地址：</p>
              <code>{{ selectedConnection.callback_url }}</code>
              <p>仅切换为“平台代管”不会执行该操作。</p>
            </div>

            <div v-if="actionError" class="inline-error" role="alert">
              {{ actionError.message }}
              <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
            </div>
          </div>

          <footer class="wechat-dialog-actions">
            <button class="button button--secondary" type="button" :disabled="Boolean(activeAction)" @click="closeDialog">
              取消
            </button>
            <button
              class="button button--primary"
              type="submit"
              :disabled="Boolean(activeAction) || (dialog === 'token' && !tokenDraft)"
            >
              {{
                activeAction
                  ? "正在提交"
                  : dialog === "token"
                    ? "更换 Token"
                    : "确认并应用"
              }}
            </button>
          </footer>
        </form>
      </div>
    </Teleport>
  </div>
</template>
