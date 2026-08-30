<script setup lang="ts">
import { Check, Copy, KeyRound, Plus, Server, X } from "lucide-vue-next";
import { reactive, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";
import { managementApi } from "@/api/resources";
import type { CreateConnectionInput, GeweConnection } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime } from "@/utils/format";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<GeweConnection>(
  managementApi.connections.list,
);

const showForm = ref(false);
const submitting = ref(false);
const formError = shallowRef<ApiError | null>(null);
const copiedId = ref<string | null>(null);
const createdId = ref<string | null>(null);

const initialDraft: CreateConnectionInput = {
  name: "",
  base_url: "",
  token: "",
  callback_mode: "MANUAL",
};
const draft = reactive<CreateConnectionInput>({ ...initialDraft });

function resetForm() {
  Object.assign(draft, initialDraft);
  formError.value = null;
}

function closeForm() {
  showForm.value = false;
  resetForm();
}

async function createConnection() {
  submitting.value = true;
  formError.value = null;
  try {
    const connection = await managementApi.connections.create({
      name: draft.name.trim(),
      base_url: draft.base_url.trim(),
      token: draft.token,
      callback_mode: draft.callback_mode,
    });
    createdId.value = connection.id;
    draft.token = "";
    showForm.value = false;
    await reload();
    resetForm();
  } catch (caught) {
    formError.value =
      caught instanceof ApiError
        ? caught
        : new ApiError("创建连接时发生未知错误", 0, "UNKNOWN_ERROR");
  } finally {
    submitting.value = false;
  }
}

async function copyCallback(connection: GeweConnection) {
  if (!connection.callback_url) return;
  await navigator.clipboard.writeText(connection.callback_url);
  copiedId.value = connection.id;
  window.setTimeout(() => {
    if (copiedId.value === connection.id) copiedId.value = null;
  }, 1800);
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="GeWe Connection" description="管理 Token、API 地址与每个 Token 唯一的回调入口">
      <template #actions>
        <button class="button button--primary" type="button" @click="showForm = !showForm">
          <X v-if="showForm" :size="16" />
          <Plus v-else :size="16" />
          {{ showForm ? "收起" : "新建连接" }}
        </button>
      </template>
    </PageHeader>

    <section v-if="showForm" class="form-panel" aria-labelledby="connection-form-title">
      <div class="form-panel-header">
        <div>
          <h3 id="connection-form-title">新建 GeWe Connection</h3>
          <p>Token 保存后仅显示掩码</p>
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
            maxlength="80"
            autocomplete="off"
            placeholder="例如：主连接"
          />
        </label>
        <label class="field-control">
          <span>GeWe API 地址</span>
          <input
            v-model="draft.base_url"
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

        <fieldset class="field-control field-control--wide">
          <legend>回调管理模式</legend>
          <div class="segmented-control">
            <label :class="{ active: draft.callback_mode === 'MANUAL' }">
              <input v-model="draft.callback_mode" type="radio" value="MANUAL" />
              手动管理
            </label>
            <label :class="{ active: draft.callback_mode === 'PLATFORM_MANAGED' }">
              <input v-model="draft.callback_mode" type="radio" value="PLATFORM_MANAGED" />
              平台代管
            </label>
          </div>
          <small v-if="draft.callback_mode === 'PLATFORM_MANAGED'">
            创建连接不会自动覆盖 GeWe 回调，后续需单独确认设置。
          </small>
        </fieldset>

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
          <button class="button button--primary" type="button" @click="showForm = true">
            <Plus :size="16" />
            新建连接
          </button>
        </template>
      </EmptyState>

      <div v-else class="table-scroll">
        <table class="data-table connection-table">
          <thead>
            <tr>
              <th>连接</th>
              <th>状态</th>
              <th>Token</th>
              <th>回调地址</th>
              <th>账号</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="connection in items" :key="connection.id" :class="{ 'row-created': createdId === connection.id }">
              <td>
                <div class="primary-cell">
                  <span class="resource-icon"><Server :size="17" /></span>
                  <span>
                    <strong>{{ connection.name }}</strong>
                    <small>{{ connection.base_url }}</small>
                  </span>
                </div>
              </td>
              <td>
                <StatusBadge :status="connection.health_status" />
              </td>
              <td><code>{{ connection.token_masked || "-" }}</code></td>
              <td>
                <div v-if="connection.callback_url" class="callback-cell">
                  <span>
                    <code>{{ connection.callback_url }}</code>
                    <small>{{ connection.callback_mode === "MANUAL" ? "手动管理" : "平台代管" }}</small>
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
                <span v-else class="muted-text">-</span>
              </td>
              <td>{{ connection.account_count ?? "-" }}</td>
              <td>{{ formatDateTime(connection.updated_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
