<script setup lang="ts">
import { ArrowRight, Bot, Eye, EyeOff, LockKeyhole } from "lucide-vue-next";
import { computed, reactive, ref, shallowRef } from "vue";
import { useRoute, useRouter } from "vue-router";

import { ApiError } from "@/api/client";
import { authSession } from "@/auth/session";

const route = useRoute();
const router = useRouter();
const form = reactive({ username: "", password: "" });
const submitting = ref(false);
const showPassword = ref(false);
const error = shallowRef<ApiError | null>(null);
const initialized = computed(() => route.query.initialized === "1");

function redirectTarget(): string {
  const target = typeof route.query.redirect === "string" ? route.query.redirect : "/";
  if (!target.startsWith("/") || target.startsWith("//") || ["/login", "/bootstrap"].includes(target)) {
    return "/";
  }
  return target;
}

function loginMessage(caught: ApiError): string {
  if (caught.status === 401) return "用户名或密码错误";
  if (caught.status === 429) return "登录尝试过于频繁，请稍后再试";
  if (caught.status === 403) return "登录安全校验失败，请重新提交";
  return caught.message;
}

async function submit() {
  submitting.value = true;
  error.value = null;
  try {
    await authSession.login({
      username: form.username.trim(),
      password: form.password,
    });
    await router.replace(redirectTarget());
  } catch (caught) {
    const apiError =
      caught instanceof ApiError ? caught : new ApiError("登录时发生未知错误", 0, "UNKNOWN_ERROR");
    error.value = new ApiError(loginMessage(apiError), apiError.status, apiError.code, apiError.traceId);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <main class="auth-page">
    <section class="auth-panel" aria-labelledby="login-title">
      <div class="auth-brand">
        <span class="brand-mark"><Bot :size="21" stroke-width="1.8" /></span>
        <span>
          <strong>微信机器人</strong>
          <small>管理平台</small>
        </span>
      </div>

      <div class="auth-heading">
        <h1 id="login-title">登录管理后台</h1>
        <p>使用管理员账号继续</p>
      </div>

      <div v-if="initialized" class="auth-success" role="status">
        初始管理员已创建，现在可以登录。
      </div>

      <form class="auth-form" @submit.prevent="submit">
        <label class="field-control">
          <span>用户名</span>
          <input
            v-model="form.username"
            type="text"
            required
            maxlength="80"
            autocomplete="username"
            autofocus
          />
        </label>

        <label class="field-control">
          <span>密码</span>
          <span class="password-input">
            <LockKeyhole :size="16" aria-hidden="true" />
            <input
              v-model="form.password"
              :type="showPassword ? 'text' : 'password'"
              required
              autocomplete="current-password"
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

        <div v-if="error" class="inline-error" role="alert">
          <span>{{ error.message }}</span>
          <code v-if="error.traceId">Trace {{ error.traceId }}</code>
        </div>

        <button class="button button--primary auth-submit" type="submit" :disabled="submitting">
          {{ submitting ? "正在登录" : "登录" }}
          <ArrowRight v-if="!submitting" :size="16" />
        </button>
      </form>

      <RouterLink class="auth-link" to="/bootstrap">首次初始化管理员</RouterLink>
    </section>
  </main>
</template>
