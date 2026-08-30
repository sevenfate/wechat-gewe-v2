import { reactive } from "vue";

import { authApi } from "@/api/auth";
import type { AuthUser, BootstrapInput, LoginInput } from "@/api/types";

const CSRF_COOKIE_NAME = "wechat_bot_csrf";

interface SessionState {
  user: AuthUser | null;
  idleExpiresAt: string | null;
  absoluteExpiresAt: string | null;
  restored: boolean;
  restoring: boolean;
}

const state = reactive<SessionState>({
  user: null,
  idleExpiresAt: null,
  absoluteExpiresAt: null,
  restored: false,
  restoring: false,
});

let csrfToken = readCookie(CSRF_COOKIE_NAME);
let restoreTask: Promise<boolean> | null = null;

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const entry = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  if (!entry) return null;
  try {
    return decodeURIComponent(entry.slice(prefix.length));
  } catch {
    return entry.slice(prefix.length);
  }
}

function clearLocalSession() {
  state.user = null;
  state.idleExpiresAt = null;
  state.absoluteExpiresAt = null;
  csrfToken = null;
}

async function restore(): Promise<boolean> {
  if (state.restored) return state.user !== null;
  if (restoreTask) return restoreTask;

  state.restoring = true;
  csrfToken = readCookie(CSRF_COOKIE_NAME);
  restoreTask = (async () => {
    try {
      state.user = await authApi.me();
      csrfToken = readCookie(CSRF_COOKIE_NAME);
      return true;
    } catch {
      clearLocalSession();
      return false;
    } finally {
      state.restored = true;
      state.restoring = false;
      restoreTask = null;
    }
  })();
  return restoreTask;
}

async function login(input: LoginInput) {
  const preAuth = await authApi.csrf();
  csrfToken = preAuth.csrf_token;
  const session = await authApi.login(input, preAuth.csrf_token);
  state.user = session.user;
  state.idleExpiresAt = session.idle_expires_at;
  state.absoluteExpiresAt = session.absolute_expires_at;
  state.restored = true;
  csrfToken = session.csrf_token;
  return session.user;
}

async function bootstrap(input: BootstrapInput, bootstrapToken: string) {
  return authApi.bootstrap(input, bootstrapToken);
}

async function logout() {
  await authApi.logout();
  clearLocalSession();
  state.restored = true;
}

function invalidate() {
  clearLocalSession();
  state.restored = true;
}

function getCsrfToken(): string | null {
  return csrfToken;
}

export const authSession = {
  state,
  restore,
  login,
  bootstrap,
  logout,
  invalidate,
  getCsrfToken,
};
