import { apiRequest, AUTH_BASE_URL } from "./client";
import type { AuthUser, BootstrapInput, LoginInput, LoginSession } from "./types";

interface CsrfResponse {
  csrf_token: string;
}

interface MessageResponse {
  message: string;
}

export const authApi = {
  csrf: () =>
    apiRequest<CsrfResponse>("/csrf", {}, { baseUrl: AUTH_BASE_URL, skipUnauthorized: true }),
  bootstrap: (input: BootstrapInput, bootstrapToken: string) =>
    apiRequest<AuthUser>(
      "/bootstrap",
      {
        method: "POST",
        headers: { "X-Bootstrap-Token": bootstrapToken },
        body: JSON.stringify(input),
      },
      { baseUrl: AUTH_BASE_URL, skipCsrf: true, skipUnauthorized: true },
    ),
  login: (input: LoginInput, csrfToken: string) =>
    apiRequest<LoginSession>(
      "/login",
      {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
        body: JSON.stringify(input),
      },
      { baseUrl: AUTH_BASE_URL, skipCsrf: true, skipUnauthorized: true },
    ),
  me: () =>
    apiRequest<AuthUser>("/me", {}, { baseUrl: AUTH_BASE_URL, skipUnauthorized: true }),
  logout: () =>
    apiRequest<MessageResponse>("/logout", { method: "POST" }, { baseUrl: AUTH_BASE_URL }),
};
