<script setup lang="ts">
import {
  Check,
  ExternalLink,
  Plus,
  Power,
  QrCode,
  RotateCcw,
  ScanLine,
  Smartphone,
  UserPlus,
  Wifi,
  X,
} from "lucide-vue-next";
import { computed, reactive, ref, shallowRef, watch } from "vue";

import { ApiError } from "@/api/client";
import { managementApi } from "@/api/resources";
import type { BotAccount, GeweConnection } from "@/api/types";
import {
  wechatOperationsApi,
  type LoginCheckResult,
  type LoginQrCodeResult,
  type WechatAccount,
} from "@/api/wechat-operations";
import { hasPermission } from "@/auth/permissions";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import IdentityCell from "@/components/IdentityCell.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useAsyncResource } from "@/composables/useAsyncResource";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime } from "@/utils/format";
import "@/styles/wechat-operations.css";

type AccountDialog = "onboard" | "qr";
type OnboardingMode = "qr" | "manual";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<BotAccount>(
  managementApi.accounts.list,
);
const {
  data: connectionData,
  loading: connectionsLoading,
  error: connectionsError,
  reload: reloadConnections,
} = useAsyncResource(() =>
  hasPermission(authSession.state.user, "connection.read")
    ? managementApi.connections.list()
    : Promise.resolve({ items: [], total: 0, next_cursor: null }),
);

const dialog = ref<AccountDialog | null>(null);
const onboardingMode = ref<OnboardingMode>("qr");
const submitting = ref(false);
const activeAction = ref("");
const actionError = shallowRef<ApiError | null>(null);
const feedback = ref("");
const feedbackLink = ref("");
const qrResult = shallowRef<LoginQrCodeResult | null>(null);
const loginResult = shallowRef<LoginCheckResult | null>(null);

const manualDraft = reactive({
  connection_id: "",
  app_id: "",
  wxid: "",
  note: "",
});
const qrDraft = reactive({
  connection_id: "",
  device_type: "mac" as "mac" | "ipad",
  region_id: "320000",
  app_id: "",
  proxy_ip: "",
  ttuid: "",
  aid: "",
});
const checkDraft = reactive({
  auto_sliding: true,
  proxy_ip: "",
  captcha_code: "",
});

const connections = computed(() => connectionData.value?.items || []);
const qrImageSource = computed(() => normalizeQrImage(qrResult.value?.qr_image_base64 || ""));
const qrTargetUrl = computed(() => safeHttpUrl(qrResult.value?.qr_data || ""));
const verificationUrl = computed(() => safeHttpUrl(loginResult.value?.verification_url || ""));
const canWrite = computed(() =>
  authSession.state.user?.roles.includes("owner") === true ||
  authSession.state.user?.permissions.includes("account.write") === true,
);
const canReadConnections = computed(() =>
  hasPermission(authSession.state.user, "connection.read"),
);

watch(connectionData, (result) => {
  const available = result?.items || [];
  const preferred = available.find((item) => item.status === "ACTIVE") || available[0];
  if (!available.some((item) => item.id === manualDraft.connection_id)) {
    manualDraft.connection_id = preferred?.id || "";
  }
  if (!available.some((item) => item.id === qrDraft.connection_id)) {
    qrDraft.connection_id = preferred?.id || "";
  }
});

function asApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError(fallback, 0, "UNKNOWN_ERROR");
}

function safeHttpUrl(value: string): string {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

function normalizeQrImage(value: string): string {
  const compact = value.trim().replace(/\s/g, "");
  if (/^data:image\/(?:png|jpe?g|webp);base64,[a-z0-9+/=]+$/i.test(compact)) {
    return compact;
  }
  if (/^[a-z0-9+/=]+$/i.test(compact) && compact.length >= 16) {
    return `data:image/png;base64,${compact}`;
  }
  return "";
}

function connectionLabel(connection: GeweConnection): string {
  return `${connection.name}${connection.status === "DISABLED" ? "（已停用）" : ""}`;
}

function connectionName(connectionId: string): string {
  return connections.value.find((item) => item.id === connectionId)?.name || "未知连接";
}

function replaceAccount(updated: WechatAccount) {
  items.value = items.value.map((item) => (item.id === updated.id ? updated : item));
}

function isAccountBusy(accountId: string): boolean {
  return activeAction.value.startsWith(`${accountId}:`);
}

function resetFeedback() {
  actionError.value = null;
  feedback.value = "";
  feedbackLink.value = "";
}

function resetOnboarding() {
  manualDraft.app_id = "";
  manualDraft.wxid = "";
  manualDraft.note = "";
  qrDraft.app_id = "";
  qrDraft.proxy_ip = "";
  qrDraft.ttuid = "";
  qrDraft.aid = "";
  qrResult.value = null;
  loginResult.value = null;
  checkDraft.auto_sliding = true;
  checkDraft.proxy_ip = "";
  checkDraft.captcha_code = "";
}

function openOnboarding(mode: OnboardingMode = "qr") {
  if (!canWrite.value || !connections.value.length) return;
  onboardingMode.value = mode;
  dialog.value = "onboard";
  resetFeedback();
  resetOnboarding();
}

function changeOnboardingConnection(event: Event) {
  const connectionId = (event.currentTarget as HTMLSelectElement).value;
  if (onboardingMode.value === "qr") {
    qrDraft.connection_id = connectionId;
  } else {
    manualDraft.connection_id = connectionId;
  }
}

function closeDialog() {
  if (submitting.value) return;
  dialog.value = null;
  resetOnboarding();
  actionError.value = null;
}

async function submitOnboarding() {
  if (!canWrite.value) return;
  submitting.value = true;
  resetFeedback();
  try {
    if (onboardingMode.value === "manual") {
      const account = await wechatOperationsApi.accounts.registerManual(manualDraft.connection_id, {
        app_id: manualDraft.app_id,
        wxid: manualDraft.wxid,
        note: manualDraft.note,
      });
      feedback.value = `账号 ${account.app_id} 已登记到“${connectionName(account.gewe_connection_id)}”。`;
      dialog.value = null;
      resetOnboarding();
      await reload();
    } else {
      qrResult.value = await wechatOperationsApi.accounts.getLoginQrCode(qrDraft.connection_id, {
        device_type: qrDraft.device_type,
        region_id: qrDraft.region_id,
        app_id: qrDraft.app_id,
        proxy_ip: qrDraft.proxy_ip,
        ttuid: qrDraft.ttuid,
        aid: qrDraft.aid,
      });
      checkDraft.proxy_ip = qrDraft.proxy_ip;
      dialog.value = "qr";
      await reload();
    }
  } catch (caught) {
    actionError.value = asApiError(
      caught,
      onboardingMode.value === "manual" ? "登记微信账号失败" : "获取登录二维码失败",
    );
  } finally {
    submitting.value = false;
  }
}

async function checkQrLogin() {
  if (!canWrite.value || !qrResult.value) return;
  submitting.value = true;
  actionError.value = null;
  try {
    const result = await wechatOperationsApi.accounts.checkLogin(qrResult.value.account.id, {
      auto_sliding: checkDraft.auto_sliding,
      proxy_ip: checkDraft.proxy_ip.trim() || undefined,
      captcha_code: checkDraft.captcha_code.trim() || undefined,
    });
    loginResult.value = result;
    qrResult.value = { ...qrResult.value, account: result.account };
    replaceAccount(result.account);
    if (result.account.status === "ONLINE") {
      feedback.value = `“${result.account.nickname || result.account.app_id}”已登录。`;
    }
  } catch (caught) {
    actionError.value = asApiError(caught, "检查登录状态失败");
  } finally {
    submitting.value = false;
  }
}

function prepareNewQr() {
  const account = qrResult.value?.account;
  if (!account) return;
  qrDraft.connection_id = account.gewe_connection_id;
  qrDraft.app_id = account.app_id;
  onboardingMode.value = "qr";
  dialog.value = "onboard";
  qrResult.value = null;
  loginResult.value = null;
  actionError.value = null;
}

async function checkLogin(account: BotAccount) {
  if (!canWrite.value) return;
  activeAction.value = `${account.id}:login`;
  resetFeedback();
  try {
    const result = await wechatOperationsApi.accounts.checkLogin(account.id);
    replaceAccount(result.account);
    feedback.value = `“${result.account.nickname || result.account.app_id}”登录状态：${loginStatusLabel(result.login_status)}。`;
    feedbackLink.value = safeHttpUrl(result.verification_url || "");
  } catch (caught) {
    actionError.value = asApiError(caught, "检查登录状态失败");
  } finally {
    activeAction.value = "";
  }
}

async function checkOnline(account: BotAccount) {
  if (!canWrite.value) return;
  activeAction.value = `${account.id}:online`;
  resetFeedback();
  try {
    const result = await wechatOperationsApi.accounts.checkOnline(account.id);
    replaceAccount(result.account);
    feedback.value = `“${result.account.nickname || result.account.app_id}”当前${result.online ? "在线" : "离线"}。`;
  } catch (caught) {
    actionError.value = asApiError(caught, "在线检查失败");
  } finally {
    activeAction.value = "";
  }
}

async function reconnect(account: BotAccount) {
  if (!canWrite.value) return;
  activeAction.value = `${account.id}:reconnect`;
  resetFeedback();
  try {
    const result = await wechatOperationsApi.accounts.reconnect(account.id);
    replaceAccount(result.account);
    feedback.value = `“${result.account.nickname || result.account.app_id}”重连结果：${loginStatusLabel(result.login_status)}。`;
  } catch (caught) {
    actionError.value = asApiError(caught, "账号重连失败");
  } finally {
    activeAction.value = "";
  }
}

async function toggleDisabled(account: BotAccount) {
  if (!canWrite.value) return;
  const disabled = account.status !== "DISABLED";
  activeAction.value = `${account.id}:disabled`;
  resetFeedback();
  try {
    const updated = await wechatOperationsApi.accounts.setDisabled(account.id, disabled);
    replaceAccount(updated);
    feedback.value = `“${updated.nickname || updated.app_id}”已${disabled ? "停用" : "启用"}。`;
  } catch (caught) {
    actionError.value = asApiError(caught, "修改账号状态失败");
  } finally {
    activeAction.value = "";
  }
}

function loginStatusLabel(status: number | null): string {
  if (status === 0) return "等待扫码";
  if (status === 1) return "已扫码，等待确认";
  if (status === 2) return "登录成功";
  return "等待上游返回";
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="微信账号" description="接入账号并执行登录、在线检查和重连">
      <template #actions>
        <button
          class="button button--primary"
          type="button"
          :disabled="!canWrite || !canReadConnections || connectionsLoading || !connections.length"
          :title="
            !canReadConnections
              ? '接入账号还需要 connection.read 权限'
              : connections.length
                ? '接入微信账号'
                : '请先创建 GeWe Connection'
          "
          @click="openOnboarding()"
        >
          <Plus :size="16" />
          接入账号
        </button>
      </template>
    </PageHeader>

    <section v-if="!canReadConnections" class="notice-band notice-band--neutral" role="status">
      当前角色可查看微信账号；连接名称与新账号接入需要 <code>connection.read</code> 权限。
    </section>
    <ErrorState v-if="connectionsError" :error="connectionsError" @retry="reloadConnections" />
    <div v-if="feedback" class="wechat-feedback wechat-feedback--success" role="status">
      <Check :size="17" />
      <span>{{ feedback }}</span>
      <a v-if="feedbackLink" :href="feedbackLink" target="_blank" rel="noopener noreferrer">
        完成验证 <ExternalLink :size="14" />
      </a>
      <button class="icon-button icon-button--small" type="button" aria-label="关闭提示" @click="resetFeedback">
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

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索昵称、wxid 或 appId"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState
        v-else-if="!items.length"
        :title="search ? '没有匹配的微信账号' : '暂无微信账号'"
        :detail="!search && !connections.length ? '请先创建 GeWe Connection' : undefined"
      >
        <template #icon><Smartphone :size="23" /></template>
        <template v-if="!search && connections.length" #action>
          <button class="button button--primary" type="button" :disabled="!canWrite" @click="openOnboarding()">
            <QrCode :size="16" />扫码接入
          </button>
        </template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table wechat-account-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>在线状态</th>
              <th>连接 / appId</th>
              <th>最近检查</th>
              <th>最后在线</th>
              <th>备注</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="account in items" :key="account.id">
              <td>
                <IdentityCell
                  :name="account.nickname"
                  :secondary="account.wxid || account.alias || account.app_id"
                  :avatar-url="account.avatar_url"
                />
              </td>
              <td><StatusBadge :status="account.status" /></td>
              <td>
                <span class="stacked-cell">
                  <span>{{ connectionName(account.gewe_connection_id) }}</span>
                  <code>{{ account.app_id }}</code>
                </span>
              </td>
              <td>
                <span class="stacked-cell">
                  <span>{{ formatDateTime(account.last_status_checked_at) }}</span>
                  <small v-if="account.last_status_error" :title="account.last_status_error">
                    {{ account.last_status_error }}
                  </small>
                </span>
              </td>
              <td>{{ formatDateTime(account.last_online_at) }}</td>
              <td>{{ account.note || "-" }}</td>
              <td>
                <div class="wechat-row-actions">
                  <button
                    v-if="['QR_PENDING', 'SCANNED'].includes(account.status)"
                    class="button button--secondary"
                    type="button"
                    :disabled="!canWrite || isAccountBusy(account.id)"
                    @click="checkLogin(account)"
                  >
                    <ScanLine :size="15" />检查登录
                  </button>
                  <button
                    class="icon-button"
                    type="button"
                    :disabled="!canWrite || account.status === 'DISABLED' || isAccountBusy(account.id)"
                    aria-label="检查在线状态"
                    title="检查在线状态"
                    @click="checkOnline(account)"
                  >
                    <Wifi :size="16" />
                  </button>
                  <button
                    class="icon-button"
                    type="button"
                    :disabled="!canWrite || account.status === 'DISABLED' || isAccountBusy(account.id)"
                    aria-label="重连账号"
                    title="重连账号"
                    @click="reconnect(account)"
                  >
                    <RotateCcw :size="16" />
                  </button>
                  <button
                    class="button"
                    :class="account.status === 'DISABLED' ? 'button--secondary' : 'button--danger'"
                    type="button"
                    :disabled="!canWrite || isAccountBusy(account.id)"
                    @click="toggleDisabled(account)"
                  >
                    <Power :size="15" />
                    {{ account.status === "DISABLED" ? "启用" : "停用" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <Teleport to="body">
      <div v-if="dialog === 'onboard'" class="wechat-dialog-backdrop" @click.self="closeDialog">
        <form class="wechat-dialog wechat-dialog--wide" role="dialog" aria-modal="true" aria-labelledby="account-onboard-title" @submit.prevent="submitOnboarding">
          <header class="wechat-dialog-header">
            <div>
              <h3 id="account-onboard-title">接入微信账号</h3>
              <p>扫码登录或登记已有 appId</p>
            </div>
            <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeDialog">
              <X :size="18" />
            </button>
          </header>

          <div class="wechat-dialog-body">
            <div class="wechat-mode-tabs" role="tablist" aria-label="账号接入方式">
              <button
                type="button"
                role="tab"
                :aria-selected="onboardingMode === 'qr'"
                :class="{ active: onboardingMode === 'qr' }"
                @click="onboardingMode = 'qr'; actionError = null"
              >
                <QrCode :size="16" />扫码登录
              </button>
              <button
                type="button"
                role="tab"
                :aria-selected="onboardingMode === 'manual'"
                :class="{ active: onboardingMode === 'manual' }"
                @click="onboardingMode = 'manual'; actionError = null"
              >
                <UserPlus :size="16" />手工登记
              </button>
            </div>

            <label class="field-control">
              <span>GeWe Connection</span>
              <select
                :value="onboardingMode === 'qr' ? qrDraft.connection_id : manualDraft.connection_id"
                required
                @change="changeOnboardingConnection"
              >
                <option v-for="connection in connections" :key="connection.id" :value="connection.id">
                  {{ connectionLabel(connection) }}
                </option>
              </select>
            </label>

            <template v-if="onboardingMode === 'manual'">
              <div class="wechat-field-grid">
                <label class="field-control">
                  <span>appId</span>
                  <input v-model="manualDraft.app_id" type="text" required maxlength="255" autocomplete="off" />
                </label>
                <label class="field-control">
                  <span>wxid（可选）</span>
                  <input v-model="manualDraft.wxid" type="text" maxlength="255" autocomplete="off" />
                </label>
              </div>
              <label class="field-control">
                <span>备注（可选）</span>
                <textarea v-model="manualDraft.note" maxlength="500" rows="3" />
              </label>
            </template>

            <template v-else>
              <div class="wechat-field-grid">
                <fieldset class="field-control">
                  <legend>登录设备</legend>
                  <div class="segmented-control">
                    <label :class="{ active: qrDraft.device_type === 'mac' }">
                      <input v-model="qrDraft.device_type" type="radio" value="mac" />Mac
                    </label>
                    <label :class="{ active: qrDraft.device_type === 'ipad' }">
                      <input v-model="qrDraft.device_type" type="radio" value="ipad" />iPad
                    </label>
                  </div>
                </fieldset>
                <label class="field-control">
                  <span>地区 ID</span>
                  <input v-model="qrDraft.region_id" type="text" required maxlength="40" inputmode="numeric" />
                </label>
              </div>
              <details class="wechat-advanced">
                <summary>高级登录参数</summary>
                <div class="wechat-field-grid">
                  <label class="field-control">
                    <span>已有 appId（可选）</span>
                    <input v-model="qrDraft.app_id" type="text" maxlength="255" autocomplete="off" />
                  </label>
                  <label class="field-control">
                    <span>代理地址（可选）</span>
                    <input v-model="qrDraft.proxy_ip" type="text" maxlength="1000" autocomplete="off" />
                  </label>
                  <label class="field-control">
                    <span>ttuid（可选）</span>
                    <input v-model="qrDraft.ttuid" type="text" maxlength="255" autocomplete="off" />
                  </label>
                  <label class="field-control">
                    <span>aid（可选）</span>
                    <input v-model="qrDraft.aid" type="text" maxlength="255" autocomplete="off" />
                  </label>
                </div>
              </details>
            </template>

            <div v-if="actionError" class="inline-error" role="alert">
              {{ actionError.message }}
              <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
            </div>
          </div>

          <footer class="wechat-dialog-actions">
            <button class="button button--secondary" type="button" :disabled="submitting" @click="closeDialog">
              取消
            </button>
            <button class="button button--primary" type="submit" :disabled="submitting">
              {{ submitting ? "正在提交" : onboardingMode === "qr" ? "获取二维码" : "登记账号" }}
            </button>
          </footer>
        </form>
      </div>

      <div v-else-if="dialog === 'qr' && qrResult" class="wechat-dialog-backdrop" @click.self="closeDialog">
        <section class="wechat-dialog wechat-dialog--wide" role="dialog" aria-modal="true" aria-labelledby="login-qr-title">
          <header class="wechat-dialog-header">
            <div>
              <h3 id="login-qr-title">扫码登录微信</h3>
              <p>{{ qrResult.account.app_id }}</p>
            </div>
            <button class="icon-button" type="button" aria-label="关闭" title="关闭" @click="closeDialog">
              <X :size="18" />
            </button>
          </header>

          <div class="wechat-dialog-body">
            <div class="wechat-qr-layout">
              <div class="wechat-qr-media">
                <img v-if="qrImageSource" :src="qrImageSource" alt="微信登录二维码" />
                <div v-else class="wechat-qr-fallback">
                  <QrCode :size="44" />
                  <span>二维码图片不可用</span>
                </div>
                <a v-if="qrTargetUrl" :href="qrTargetUrl" target="_blank" rel="noopener noreferrer">
                  在新窗口打开 <ExternalLink :size="13" />
                </a>
              </div>

              <div class="wechat-qr-details">
                <div class="wechat-detail-row">
                  <span>账号状态</span>
                  <StatusBadge :status="qrResult.account.status" />
                </div>
                <div class="wechat-detail-row">
                  <span>二维码到期</span>
                  <strong>{{ formatDateTime(qrResult.expires_at) }}</strong>
                </div>
                <div v-if="loginResult" class="notice-band notice-band--neutral">
                  登录检查：{{ loginStatusLabel(loginResult.login_status) }}
                </div>
                <a
                  v-if="verificationUrl"
                  class="wechat-verification-link"
                  :href="verificationUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  打开辅助验证 <ExternalLink :size="14" />
                </a>
                <label class="wechat-check-option">
                  <input v-model="checkDraft.auto_sliding" type="checkbox" />
                  <span>允许滑块辅助</span>
                </label>
                <label class="field-control">
                  <span>验证码（需要时填写）</span>
                  <input v-model="checkDraft.captcha_code" type="text" maxlength="100" autocomplete="off" />
                </label>
                <label class="field-control">
                  <span>登录检查代理（可选）</span>
                  <input v-model="checkDraft.proxy_ip" type="text" maxlength="1000" autocomplete="off" />
                </label>
              </div>
            </div>

            <div v-if="actionError" class="inline-error" role="alert">
              {{ actionError.message }}
              <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
            </div>
          </div>

          <footer class="wechat-dialog-actions wechat-dialog-actions--split">
            <button class="button button--secondary" type="button" :disabled="submitting" @click="prepareNewQr">
              <RotateCcw :size="15" />重新获取
            </button>
            <span class="wechat-action-spacer" />
            <button class="button button--secondary" type="button" :disabled="submitting" @click="closeDialog">
              关闭
            </button>
            <button class="button button--primary" type="button" :disabled="submitting" @click="checkQrLogin">
              <ScanLine :size="15" />{{ submitting ? "正在检查" : "检查登录状态" }}
            </button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>
