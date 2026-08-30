<script setup lang="ts">
import { Activity, Check, PackageOpen, Play, Plus, RotateCcw, Square, X } from "lucide-vue-next";
import { computed, onMounted, reactive, ref } from "vue";

import { ApiError } from "@/api/client";
import {
  pluginManagementApi,
  type ManagedPlugin,
  type ManagedPluginCatalog,
  type ManagedPluginDeployment,
  type ManagedPluginPackage,
  type ManagedPluginRevision,
  type ManagedPluginRevisionDraft,
} from "@/api/plugin-management";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import "@/styles/plugin-management.css";

type DialogMode = "deploy" | "revision" | "activate";

const builtinPlugins = [
  { id: "builtin.echo", name: "Echo" },
  { id: "builtin.weather", name: "天气" },
  { id: "builtin.maibot-connector", name: "MaiBot Connector" },
] as const;
type BuiltinPluginId = (typeof builtinPlugins)[number]["id"];

const catalog = ref<ManagedPluginCatalog | null>(null);
const workspaces = ref<Array<{ id: string; name: string }>>([]);
const selectedWorkspaceId = ref("");
const selectedBuiltinId = ref<BuiltinPluginId>("builtin.echo");
const search = ref("");
const loading = ref(false);
const error = ref<ApiError | null>(null);
const actionError = ref<ApiError | null>(null);
const actionResult = ref("");
const submitting = ref(false);
const draftLoading = ref(false);
const dialog = ref<DialogMode | null>(null);
const selectedPlugin = ref<ManagedPlugin | null>(null);
const selectedDeployment = ref<ManagedPluginDeployment | null>(null);
const selectedRevisionId = ref("");
const selectedSourceRevisionId = ref("");
let draftRequestId = 0;
const draft = reactive({
  name: "",
  packageId: "",
  configJson: "{}",
  scopeJson: "{}",
  grants: [] as string[],
});

const isOwner = computed(() => authSession.state.user?.roles.includes("owner") === true);
const canDeploy = computed(
  () => isOwner.value || authSession.state.user?.permissions.includes("plugin.deploy") === true,
);
const canInvoke = computed(
  () => isOwner.value || authSession.state.user?.permissions.includes("plugin.invoke") === true,
);
const selectedBuiltin = computed(
  () => builtinPlugins.find((plugin) => plugin.id === selectedBuiltinId.value) || builtinPlugins[0],
);
const selectedBuiltinInstalled = computed(() => isBuiltinInstalled(selectedBuiltinId.value));
const plugins = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  const items = (catalog.value?.plugins || []).filter(
    (plugin) => !selectedWorkspaceId.value || plugin.workspace_id === selectedWorkspaceId.value,
  );
  if (!keyword) return items;
  return items.filter((plugin) =>
    [plugin.name, plugin.plugin_id, plugin.description].some((value) =>
      value.toLocaleLowerCase().includes(keyword),
    ),
  );
});

function packagesFor(plugin: ManagedPlugin): ManagedPluginPackage[] {
  return (catalog.value?.packages || [])
    .filter((item) => item.plugin_id === plugin.id)
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
}

function deploymentsFor(plugin: ManagedPlugin): ManagedPluginDeployment[] {
  return (catalog.value?.deployments || []).filter((item) => item.plugin_id === plugin.id);
}

function revisionsFor(deployment: ManagedPluginDeployment): ManagedPluginRevision[] {
  return (catalog.value?.revisions || [])
    .filter((item) => item.deployment_id === deployment.id)
    .sort((left, right) => right.revision_number - left.revision_number);
}

function activeRevision(deployment: ManagedPluginDeployment): ManagedPluginRevision | undefined {
  return revisionsFor(deployment).find((item) => item.id === deployment.active_revision_id);
}

function packageCapabilities(plugin: ManagedPlugin, packageId: string): string[] {
  return packagesFor(plugin).find((item) => item.id === packageId)?.manifest.capabilities || [];
}

function draftPackageCapabilities(plugin: ManagedPlugin): string[] {
  return packageCapabilities(plugin, draft.packageId);
}

function changeDraftPackage(plugin: ManagedPlugin) {
  const allowed = new Set(draftPackageCapabilities(plugin));
  draft.grants = draft.grants.filter((grant) => allowed.has(grant));
}

function isBuiltinInstalled(pluginId: string): boolean {
  return (catalog.value?.plugins || []).some(
    (plugin) =>
      plugin.workspace_id === selectedWorkspaceId.value && plugin.plugin_id === pluginId,
  );
}

function defaultPluginConfig(pluginId: string): Record<string, unknown> {
  if (pluginId === "builtin.echo") return { prefix: "" };
  if (pluginId === "builtin.weather") {
    return {
      geocoding_base_url: "https://geocoding-api.open-meteo.com/v1/search",
      forecast_base_url: "https://api.open-meteo.com/v1/forecast",
      timeout_seconds: 5,
    };
  }
  if (pluginId === "builtin.maibot-connector") {
    return {
      websocket_url: "wss://maibot.example.invalid/ws",
      api_key: "REPLACE_WITH_MAIBOT_API_KEY",
      client_uuid: "REPLACE_WITH_STABLE_CLIENT_UUID",
      message_ttl_seconds: 300,
      max_pending_messages: 1000,
      ack_retry_seconds: 10,
      reconnect_initial_seconds: 1,
      reconnect_max_seconds: 30,
      enable_proactive_messages: false,
    };
  }
  return {};
}

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const [nextCatalog, context] = await Promise.all([
      pluginManagementApi.catalog(),
      pluginManagementApi.context(),
    ]);
    catalog.value = nextCatalog;
    workspaces.value = [{ id: context.workspace_id, name: context.name }];
    if (!workspaces.value.some((item) => item.id === selectedWorkspaceId.value)) {
      selectedWorkspaceId.value = workspaces.value[0]?.id || "";
    }
  } catch (caught) {
    error.value = caught instanceof ApiError ? caught : new ApiError("读取插件目录失败", 0);
  } finally {
    loading.value = false;
  }
}

async function installBuiltin() {
  const builtin = selectedBuiltin.value;
  if (!selectedWorkspaceId.value || isBuiltinInstalled(builtin.id)) return;
  submitting.value = true;
  actionError.value = null;
  actionResult.value = "";
  try {
    await pluginManagementApi.installBuiltin(builtin.id, selectedWorkspaceId.value);
    actionResult.value = `${builtin.name} 已安装`;
    await load();
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught : new ApiError("安装插件失败", 0);
  } finally {
    submitting.value = false;
  }
}

async function openDialog(
  mode: DialogMode,
  plugin: ManagedPlugin,
  deployment?: ManagedPluginDeployment,
) {
  const packages = packagesFor(plugin);
  const revisions = deployment ? revisionsFor(deployment) : [];
  selectedPlugin.value = plugin;
  selectedDeployment.value = deployment || null;
  selectedRevisionId.value = deployment?.active_revision_id || revisions[0]?.id || "";
  selectedSourceRevisionId.value = selectedRevisionId.value;
  dialog.value = mode;
  actionError.value = null;
  Object.assign(draft, {
    name: deployment?.name || `${plugin.name} 默认部署`,
    packageId: packages[0]?.id || "",
    configJson: mode === "revision" ? "{}" : JSON.stringify(defaultPluginConfig(plugin.plugin_id), null, 2),
    scopeJson: JSON.stringify({ workspace_id: plugin.workspace_id }, null, 2),
    grants: [...packageCapabilities(plugin, packages[0]?.id || "")],
  });
  if (mode === "revision") await loadSelectedRevisionDraft();
}

function applyRevisionDraft(nextDraft: ManagedPluginRevisionDraft) {
  Object.assign(draft, {
    packageId: nextDraft.package_version_id,
    configJson: JSON.stringify(nextDraft.config, null, 2),
    scopeJson: JSON.stringify(nextDraft.scope, null, 2),
    grants: [...nextDraft.grants],
  });
}

async function loadSelectedRevisionDraft() {
  const deployment = selectedDeployment.value;
  const sourceRevisionId = selectedSourceRevisionId.value;
  if (!deployment || !sourceRevisionId) {
    actionError.value = new ApiError("请选择来源 Revision", 0);
    return;
  }
  const requestId = ++draftRequestId;
  draftLoading.value = true;
  actionError.value = null;
  try {
    const nextDraft = await pluginManagementApi.revisionDraft(deployment.id, sourceRevisionId);
    if (requestId !== draftRequestId || dialog.value !== "revision") return;
    applyRevisionDraft(nextDraft);
  } catch (caught) {
    if (requestId !== draftRequestId || dialog.value !== "revision") return;
    actionError.value =
      caught instanceof ApiError ? caught : new ApiError("读取 Revision 配置失败", 0);
  } finally {
    if (requestId === draftRequestId) draftLoading.value = false;
  }
}

function closeDialog() {
  if (submitting.value) return;
  draftRequestId += 1;
  draftLoading.value = false;
  dialog.value = null;
  selectedPlugin.value = null;
  selectedDeployment.value = null;
  selectedSourceRevisionId.value = "";
  actionError.value = null;
}

function parseObject(value: string, label: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function toggleGrant(grant: string, checked: boolean) {
  draft.grants = checked
    ? [...new Set([...draft.grants, grant])]
    : draft.grants.filter((item) => item !== grant);
}

async function submitDialog() {
  const mode = dialog.value;
  const plugin = selectedPlugin.value;
  if (!mode || !plugin) return;
  submitting.value = true;
  actionError.value = null;
  try {
    if (mode === "activate") {
      const deployment = selectedDeployment.value;
      if (!deployment || !selectedRevisionId.value) throw new Error("请选择要激活的 Revision");
      await pluginManagementApi.activate(deployment.id, selectedRevisionId.value);
    } else {
      const input = {
        package_version_id: draft.packageId,
        config: parseObject(draft.configJson, "配置"),
        scope: parseObject(draft.scopeJson, "作用域"),
        grants: draft.grants,
      };
      if (mode === "deploy") {
        const result = await pluginManagementApi.createDeployment({
          ...input,
          workspace_id: plugin.workspace_id,
          plugin_id: plugin.id,
          name: draft.name.trim(),
        });
        await pluginManagementApi.activate(result.deployment.id, result.revision.id);
      } else {
        const deployment = selectedDeployment.value;
        if (!deployment || !selectedSourceRevisionId.value) {
          throw new Error("缺少 Deployment 或来源 Revision");
        }
        const revision = await pluginManagementApi.createRevision(deployment.id, {
          ...input,
          source_revision_id: selectedSourceRevisionId.value,
        });
        await pluginManagementApi.activate(deployment.id, revision.id);
      }
    }
    submitting.value = false;
    closeDialog();
    actionResult.value = "插件运行状态已更新";
    await load();
  } catch (caught) {
    actionError.value =
      caught instanceof ApiError
        ? caught
        : new ApiError(caught instanceof Error ? caught.message : "插件操作失败", 0);
  } finally {
    submitting.value = false;
  }
}

async function deactivate(deployment: ManagedPluginDeployment) {
  if (!window.confirm(`停止插件部署 ${deployment.name}？`)) return;
  actionError.value = null;
  try {
    await pluginManagementApi.deactivate(deployment.id);
    actionResult.value = `${deployment.name} 已停止`;
    await load();
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught : new ApiError("停止插件失败", 0);
  }
}

async function checkHealth(deployment: ManagedPluginDeployment) {
  actionError.value = null;
  try {
    const response = await pluginManagementApi.health(deployment.id);
    actionResult.value = `${deployment.name}：${JSON.stringify(response.result)}（epoch ${response.activation_epoch}）`;
  } catch (caught) {
    actionError.value = caught instanceof ApiError ? caught : new ApiError("健康检查失败", 0);
  }
}

onMounted(() => void load());
</script>

<template>
  <div class="page-stack">
    <PageHeader title="插件" description="安装、配置、热启停、升级与回滚受管插件">
      <template #actions>
        <div class="plugin-header-actions">
          <div v-if="canDeploy" class="builtin-installer">
            <select v-model="selectedBuiltinId" class="filter-select" aria-label="选择内置插件">
              <option v-for="plugin in builtinPlugins" :key="plugin.id" :value="plugin.id">
                {{ plugin.name }}{{ isBuiltinInstalled(plugin.id) ? "（已安装）" : "" }}
              </option>
            </select>
            <button
              class="button button--primary"
              type="button"
              :disabled="submitting || !selectedWorkspaceId || selectedBuiltinInstalled"
              @click="installBuiltin"
            >
              <Check v-if="selectedBuiltinInstalled" :size="16" />
              <Plus v-else :size="16" />
              {{ selectedBuiltinInstalled ? "已安装" : "安装" }}
            </button>
          </div>
        </div>
      </template>
    </PageHeader>

    <div v-if="actionResult" class="plugin-result-band">
      <Activity :size="16" /><code>{{ actionResult }}</code>
    </div>
    <div v-if="actionError && !dialog" class="inline-error" role="alert">
      {{ actionError.message }}
    </div>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="plugins.length"
        placeholder="搜索插件名称或资源 ID"
        @clear="search = ''"
        @refresh="load"
      />
      <LoadingState v-if="loading && !catalog" />
      <ErrorState v-else-if="error" :error="error" @retry="load" />
      <EmptyState v-else-if="!workspaces.length" title="请先创建 GeWe Connection">
        <template #icon><PackageOpen :size="23" /></template>
      </EmptyState>
      <EmptyState v-else-if="!plugins.length" title="当前工作区尚未安装插件">
        <template #icon><PackageOpen :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table plugin-management-table">
          <thead>
            <tr>
              <th>插件</th>
              <th>最新包</th>
              <th>Deployment</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="plugin in plugins" :key="plugin.id">
              <td>
                <div class="plugin-cell">
                  <span class="resource-icon"><PackageOpen :size="17" /></span>
                  <span>
                    <strong>{{ plugin.name }}</strong>
                    <small>{{ plugin.plugin_id }}</small>
                    <span class="plugin-description">{{ plugin.description }}</span>
                  </span>
                </div>
              </td>
              <td>
                <span class="stacked-cell">
                  <code>{{ packagesFor(plugin)[0]?.semantic_version || "-" }}</code>
                  <StatusBadge :status="packagesFor(plugin)[0]?.status" />
                </span>
              </td>
              <td>
                <div v-if="deploymentsFor(plugin).length" class="plugin-deployment-list">
                  <div
                    v-for="deployment in deploymentsFor(plugin)"
                    :key="deployment.id"
                    class="plugin-deployment-row"
                  >
                    <span class="plugin-deployment-name">
                      <strong>{{ deployment.name }}</strong>
                      <small>
                        Revision {{ activeRevision(deployment)?.revision_number || "-" }}
                        <template v-if="deployment.last_error"> · {{ deployment.last_error }}</template>
                      </small>
                    </span>
                    <StatusBadge :status="deployment.status" />
                    <span class="row-actions">
                      <button
                        v-if="canInvoke && deployment.status === 'RUNNING'"
                        class="icon-button icon-button--small"
                        type="button"
                        title="健康检查"
                        aria-label="健康检查"
                        @click="checkHealth(deployment)"
                      >
                        <Activity :size="15" />
                      </button>
                      <button
                        v-if="
                          canDeploy &&
                          revisionsFor(deployment).length &&
                          !['STARTING', 'DRAINING'].includes(deployment.status)
                        "
                        class="icon-button icon-button--small"
                        type="button"
                        title="从现有 Revision 创建并激活新 Revision"
                        aria-label="升级配置"
                        @click="openDialog('revision', plugin, deployment)"
                      >
                        <Plus :size="15" />
                      </button>
                      <button
                        v-if="
                          canDeploy &&
                          revisionsFor(deployment).length &&
                          !['STARTING', 'DRAINING'].includes(deployment.status)
                        "
                        class="icon-button icon-button--small"
                        type="button"
                        :title="
                          deployment.status === 'RUNNING'
                            ? '热切换或回滚 Revision'
                            : '选择 Revision 并启动'
                        "
                        aria-label="启动或回滚"
                        @click="openDialog('activate', plugin, deployment)"
                      >
                        <RotateCcw v-if="deployment.status === 'RUNNING'" :size="15" />
                        <Play v-else :size="15" />
                      </button>
                      <button
                        v-if="canDeploy && deployment.status === 'RUNNING'"
                        class="icon-button icon-button--small"
                        type="button"
                        title="停止"
                        aria-label="停止"
                        @click="deactivate(deployment)"
                      >
                        <Square :size="14" />
                      </button>
                    </span>
                  </div>
                </div>
                <span v-else class="muted-text">未部署</span>
              </td>
              <td>
                <button
                  v-if="canDeploy"
                  class="button button--secondary"
                  type="button"
                  @click="openDialog('deploy', plugin)"
                >
                  <Plus :size="15" />新建部署
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div
      v-if="dialog && selectedPlugin"
      class="operation-dialog-backdrop"
      @click.self="closeDialog"
    >
      <form
        class="operation-dialog operation-dialog--wide"
        role="dialog"
        aria-modal="true"
        @submit.prevent="submitDialog"
      >
        <header class="operation-dialog-header">
          <h3>
            {{
              dialog === "deploy"
                ? "新建 Deployment"
                : dialog === "revision"
                  ? "创建新 Revision"
                  : "启动或回滚"
            }}
          </h3>
          <button
            class="icon-button"
            type="button"
            aria-label="关闭"
            title="关闭"
            @click="closeDialog"
          >
            <X :size="18" />
          </button>
        </header>

        <div class="operation-dialog-body">
          <template v-if="dialog === 'activate' && selectedDeployment">
            <label class="field-control">
              <span>Revision</span>
              <select v-model="selectedRevisionId" class="filter-select" required>
                <option
                  v-for="revision in revisionsFor(selectedDeployment)"
                  :key="revision.id"
                  :value="revision.id"
                >
                  #{{ revision.revision_number }} · {{ revision.content_sha256.slice(0, 12) }}
                  {{ revision.id === selectedDeployment.active_revision_id ? "（当前）" : "" }}
                </option>
              </select>
            </label>
          </template>
          <template v-else>
            <LoadingState v-if="draftLoading" />
            <div v-else class="dialog-grid">
              <label v-if="dialog === 'deploy'" class="field-control field-control--wide">
                <span>Deployment 名称</span>
                <input v-model="draft.name" type="text" required maxlength="120" />
              </label>
              <label v-if="dialog === 'revision' && selectedDeployment" class="field-control">
                <span>来源 Revision</span>
                <select
                  v-model="selectedSourceRevisionId"
                  class="filter-select"
                  aria-label="来源 Revision"
                  required
                  @change="loadSelectedRevisionDraft"
                >
                  <option
                    v-for="revision in revisionsFor(selectedDeployment)"
                    :key="revision.id"
                    :value="revision.id"
                  >
                    #{{ revision.revision_number }} · {{ revision.content_sha256.slice(0, 12) }}
                    {{ revision.id === selectedDeployment.active_revision_id ? "（当前）" : "" }}
                  </option>
                </select>
              </label>
              <label class="field-control">
                <span>插件包</span>
                <select
                  v-model="draft.packageId"
                  class="filter-select"
                  aria-label="插件包"
                  required
                  @change="changeDraftPackage(selectedPlugin)"
                >
                  <option
                    v-for="item in packagesFor(selectedPlugin)"
                    :key="item.id"
                    :value="item.id"
                  >
                    {{ item.semantic_version }} · {{ item.status }}
                  </option>
                </select>
              </label>
              <fieldset class="field-control">
                <legend>Capabilities</legend>
                <label
                  v-for="grant in draftPackageCapabilities(selectedPlugin)"
                  :key="grant"
                  class="check-option"
                >
                  <input
                    type="checkbox"
                    :checked="draft.grants.includes(grant)"
                    @change="toggleGrant(grant, ($event.target as HTMLInputElement).checked)"
                  />
                  <code>{{ grant }}</code>
                </label>
              </fieldset>
              <label class="field-control field-control--wide json-field">
                <span>配置 JSON</span>
                <textarea v-model="draft.configJson" required spellcheck="false" />
              </label>
              <label class="field-control field-control--wide json-field">
                <span>作用域 JSON</span>
                <textarea v-model="draft.scopeJson" required spellcheck="false" />
              </label>
            </div>
          </template>
          <div v-if="actionError" class="inline-error" role="alert">{{ actionError.message }}</div>
        </div>

        <footer class="operation-dialog-actions">
          <button
            class="button button--secondary"
            type="button"
            :disabled="submitting"
            @click="closeDialog"
          >
            取消
          </button>
          <button
            class="button button--primary"
            type="submit"
            :disabled="submitting || draftLoading"
          >
            {{
              submitting
                ? "正在执行"
                : dialog === "activate"
                  ? selectedDeployment?.status === "RUNNING"
                    ? "热切换"
                    : "启动"
                  : "创建并激活"
            }}
          </button>
        </footer>
      </form>
    </div>
  </div>
</template>
