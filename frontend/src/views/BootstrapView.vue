<script setup lang="ts">
import { ArrowLeft, Bot, Eye, EyeOff, KeyRound, UserRoundPlus } from "lucide-vue-next";
import { reactive, ref, shallowRef } from "vue";
import { useRouter } from "vue-router";

import { ApiError } from "@/api/client";
import { authSession } from "@/auth/session";

const router = useRouter();
const form = reactive({
  bootstrapToken: "",
  username: "",
  displayName: "",
  password: "",
  confirmPassword: "",
});
const submitting = ref(false);
const showPassword = ref(false);
const error = shallowRef<ApiError | null>(null);

function bootstrapMessage(caught: ApiError): string {
  if (caught.status === 409) return "系统已经完成初始化，请直接登录";
  if (caught.status === 503) return "服务端尚未配置 Bootstrap Token";
  if (caught.status === 403) return "Bootstrap Token 无效";
  if (caught.status === 422) return "账号信息不符合要求，请检查用户名和密码长度";
  return caught.message;
}

async function submit() {
  error.value = null;
  if (form.password !== form.confirmPassword) {
    error.value = new ApiError("两次输入的密码不一致", 0, "PASSWORD_MISMATCH");
    return;
  }

  submitting.value = true;
  try {
    await authSession.bootstrap(
      {
        username: form.username.trim(),
        display_name: form.displayName.trim() || undefined,
        password: form.password,
      },
      form.bootstrapToken,
    );
    await router.replace({ name: "login", query: { initialized: "1" } });
  } catch (caught) {
    const apiError =
      caught instanceof ApiError
        ? caught
        : new ApiError("初始化时发生未知错误", 0, "UNKNOWN_ERROR");
    error.value = new ApiError(
      bootstrapMessage(apiError),
      apiError.status,
      apiError.code,
      apiError.traceId,
    );
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-panel auth-panel--wide" aria-labelledby="bootstrap-title">
      <div class="auth-brand">
        <span class="brand-mark"><Bot :size="21" stroke-width="1.8" /></span>
        <span>
          <strong>微信机器人</strong>
          <small>管理平台</small>
        </span>
      </div>

      <div class="auth-heading">
        <h1 id="bootstrap-title">首次初始化</h1>
        <p>创建系统的初始管理员</p>
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <label class="field-control">
          <span>Bootstrap Token</span>
          <span class="password-input">
            <KeyRound :size="16" aria-hidden="true" />
            <input
              v-model="form.bootstrapToken"
              type="password"
              required
              autocomplete="off"
            />
          </span>
        </label>

        <div class="auth-field-grid">
          <label class="field-control">
            <span>用户名</span>
            <input
              v-model="form.username"
              type="text"
              required
              minlength="3"
              maxlength="80"
              autocomplete="username"
            />
          </label>
          <label class="field-control">
            <span>显示名称 <small>可选</small></span>
            <input v-model="form.displayName" type="text" maxlength="120" autocomplete="name" />
          </label>
        </div>

        <label class="field-control">
          <span>管理员密码</span>
          <span class="password-input">
            <UserRoundPlus :size="16" aria-hidden="true" />
            <input
              v-model="form.password"
              :type="showPassword ? 'text' : 'password'"
              required
              minlength="12"
              autocomplete="new-password"
            />
            <button
              class="icon-button icon-button--small"
              type="button"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              :title="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              <EyeOff v-if="showPassword" :size="16" />
              <Eye v-else :size="16" />
            </button>
          </span>
        </label>

        <label class="field-control">
          <span>确认密码</span>
          <input
            v-model="form.confirmPassword"
            :type="showPassword ? 'text' : 'password'"
            required
            minlength="12"
            autocomplete="new-password"
          />
        </label>

        <div v-if="error" class="inline-error" role="alert">
          <span>{{ error.message }}</span>
          <code v-if="error.traceId">Trace {{ error.traceId }}</code>
        </div>

        <button class="button button--primary auth-submit" type="submit" :disabled="submitting">
          {{ submitting ? "正在初始化" : "创建初始管理员" }}
        </button>
      </form>

      <RouterLink class="auth-link auth-link--back" to="/login">
        <ArrowLeft :size="15" />
        返回登录
      </RouterLink>
    </section>
  </main>
</template>
