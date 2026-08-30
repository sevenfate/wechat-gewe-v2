const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL || "/api/v1";
export const API_BASE_URL = configuredBaseUrl.replace(/\/$/, "");

interface ErrorPayload {
  message?: string;
  detail?: string | { message?: string };
  error?: string | { message?: string };
  code?: string;
  trace_id?: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly traceId?: string;

  constructor(message: string, status: number, code?: string, traceId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }
}

function errorMessage(payload: ErrorPayload | null, status: number): string {
  if (typeof payload?.detail === "string") return payload.detail;
  if (typeof payload?.detail === "object" && payload.detail?.message) return payload.detail.message;
  if (typeof payload?.error === "string") return payload.error;
  if (typeof payload?.error === "object" && payload.error?.message) return payload.error.message;
  if (payload?.message) return payload.message;
  return `请求失败（HTTP ${status}）`;
}

function unwrapData<T>(payload: unknown): T {
  if (payload && typeof payload === "object" && "data" in payload) {
    return (payload as { data: T }).data;
  }
  return payload as T;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`, {
      credentials: "same-origin",
      ...init,
      headers,
    });
  } catch {
    throw new ApiError("无法连接管理 API", 0, "NETWORK_ERROR");
  }

  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;

  if (!response.ok) {
    const errorPayload = (payload || null) as ErrorPayload | null;
    throw new ApiError(
      errorMessage(errorPayload, response.status),
      response.status,
      errorPayload?.code,
      errorPayload?.trace_id || response.headers.get("x-trace-id") || undefined,
    );
  }

  if (response.status === 204) return undefined as T;
  return unwrapData<T>(payload);
}

export function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now()}-${crypto.getRandomValues(new Uint32Array(2)).join("-")}`;
}

export function toQuery(params: Record<string, string | number | null | undefined>): string {
  const searchParams = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") {
      searchParams.set(key, String(value));
    }
  }
  const query = searchParams.toString();
  return query ? `?${query}` : "";
}
