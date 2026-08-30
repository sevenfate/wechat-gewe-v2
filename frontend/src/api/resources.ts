import { apiRequest, createIdempotencyKey, toQuery } from "./client";
import type {
  BotAccount,
  Contact,
  CreateConnectionInput,
  DiscoveredGroup,
  GeweConnection,
  ListResult,
  OverviewData,
  PermissionMatrix,
  PluginSummary,
} from "./types";

interface RawList<T> {
  items?: T[];
  results?: T[];
  next_cursor?: string | null;
  total?: number | null;
}

function normalizeList<T>(payload: RawList<T> | T[]): ListResult<T> {
  if (Array.isArray(payload)) {
    return { items: payload, next_cursor: null, total: payload.length };
  }
  const items = payload.items || payload.results || [];
  return {
    items,
    next_cursor: payload.next_cursor ?? null,
    total: payload.total ?? null,
  };
}

async function list<T>(path: string, search = ""): Promise<ListResult<T>> {
  return normalizeList(await apiRequest<RawList<T> | T[]>(`${path}${toQuery({ search })}`));
}

export const managementApi = {
  overview: () => apiRequest<OverviewData>("/overview"),
  connections: {
    list: (search = "") => list<GeweConnection>("/connections", search),
    create: (input: CreateConnectionInput) =>
      apiRequest<GeweConnection>("/connections", {
        method: "POST",
        headers: { "Idempotency-Key": createIdempotencyKey() },
        body: JSON.stringify(input),
      }),
  },
  accounts: {
    list: (search = "") => list<BotAccount>("/bot-accounts", search),
  },
  contacts: {
    list: (search = "") => list<Contact>("/contacts", search),
  },
  groups: {
    list: (search = "") => list<DiscoveredGroup>("/chatrooms", search),
  },
  plugins: {
    list: (search = "") => list<PluginSummary>("/plugins", search),
  },
  permissions: {
    matrix: () => apiRequest<PermissionMatrix>("/permissions/matrix"),
  },
};
