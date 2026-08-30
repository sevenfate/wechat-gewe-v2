import { ApiError, apiRequest, toQuery } from "./client";

const TASK_AGENT_PREFIX = "/task-agent";

export type AgentRunStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "WAITING_USER"
  | "PAUSED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED"
  | "EXPIRED";

export type AgentInboxKind = "RUN_REQUEST" | "QUESTION_ANSWER";

export type AgentEventType =
  | "SESSION_CREATED"
  | "RUN_CREATED"
  | "RUN_STATUS_CHANGED"
  | "QUESTION_ASKED"
  | "QUESTION_ANSWERED";

export type PendingQuestionStatus = "PENDING" | "ANSWERED" | "EXPIRED" | "CANCELLED";

export interface AgentListResponse<T> {
  items: T[];
  total: number;
}

export interface AgentContext {
  workspace_id: string;
  workspace_name: string;
}

export interface AgentDefinition {
  id: string;
  workspace_id: string;
  definition_key: string;
  name: string;
  description: string;
  retired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentVersion {
  id: string;
  definition_id: string;
  version_number: number;
  specification: Record<string, unknown>;
  specification_sha256: string;
  published_by_principal_id: string | null;
  published_at: string;
}

export interface AgentSession {
  id: string;
  workspace_id: string;
  agent_version_id: string;
  requester_principal_id: string;
  task_scope: Record<string, unknown>;
  task_scope_sha256: string;
  last_inbox_seq: number;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
}

export interface AgentRun {
  id: string;
  session_id: string;
  idempotency_key: string;
  input_payload: Record<string, unknown>;
  input_sha256: string;
  status: AgentRunStatus;
  started_at: string | null;
  finished_at: string | null;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentSessionInboxItem {
  id: string;
  session_id: string;
  seq: number;
  kind: AgentInboxKind;
  run_id: string;
  actor_principal_id: string;
  question_id: string | null;
  payload: Record<string, unknown>;
  payload_sha256: string;
  created_at: string;
}

export interface AgentEvent {
  id: string;
  session_id: string;
  seq: number;
  run_id: string | null;
  question_id: string | null;
  event_type: AgentEventType;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface PendingQuestion {
  id: string;
  session_id: string;
  run_id: string;
  allowed_principal_id: string;
  prompt: string;
  context: Record<string, unknown>;
  status: PendingQuestionStatus;
  expires_at: string;
  answered_at: string | null;
  answered_by_principal_id: string | null;
  answer_payload: Record<string, unknown> | null;
  answer_sha256: string | null;
  answer_inbox_seq: number | null;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentSessionState {
  session: AgentSession;
  active_run: AgentRun | null;
  inbox: AgentSessionInboxItem[];
  events: AgentEvent[];
  questions: PendingQuestion[];
  inbox_has_more: boolean;
  events_has_more: boolean;
  questions_has_more: boolean;
}

export interface QuestionAnswerResult {
  question: PendingQuestion;
  inbox_item: AgentSessionInboxItem;
  run: AgentRun;
}

export interface CreateAgentDefinitionInput {
  workspace_id: string;
  definition_key: string;
  name: string;
  description?: string;
}

export interface PublishAgentVersionInput {
  specification: Record<string, unknown>;
}

export interface CreateAgentSessionInput {
  workspace_id: string;
  agent_version_id: string;
  task_scope: Record<string, unknown>;
}

export interface CreateAgentRunInput {
  idempotency_key: string;
  input_payload: Record<string, unknown>;
}

export interface TransitionAgentRunInput {
  status: AgentRunStatus;
  reason?: string | null;
  error_code?: string | null;
}

export interface CreatePendingQuestionInput {
  allowed_principal_id: string;
  prompt: string;
  context: Record<string, unknown>;
  expires_at: string;
}

export interface OverridePendingQuestionInput {
  answer_payload: Record<string, unknown>;
  reason: string;
}

const AGENT_PAGE_SIZE = 200;
const MAX_AGENT_PAGES = 1_000;

function resourcePath(resource: string, id: string): string {
  return `${TASK_AGENT_PREFIX}/${resource}/${encodeURIComponent(id)}`;
}

async function listAll<T extends { id: string }>(path: string): Promise<AgentListResponse<T>> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const items: T[] = [];
    const seenIds = new Set<string>();
    let offset = 0;
    let expectedTotal: number | null = null;
    let firstId: string | null = null;
    let unstable = false;

    for (let pageNumber = 0; pageNumber < MAX_AGENT_PAGES; pageNumber += 1) {
      const page = await apiRequest<AgentListResponse<T>>(
        `${path}${toQuery({ limit: AGENT_PAGE_SIZE, offset })}`,
      );
      expectedTotal ??= page.total;
      firstId ??= page.items[0]?.id || null;
      if (page.total !== expectedTotal || page.items.some((item) => seenIds.has(item.id))) {
        unstable = true;
        break;
      }

      for (const item of page.items) seenIds.add(item.id);
      items.push(...page.items);
      offset += page.items.length;

      if (offset >= expectedTotal) {
        const verification = await apiRequest<AgentListResponse<T>>(
          `${path}${toQuery({ limit: 1, offset: 0 })}`,
        );
        if (verification.total === expectedTotal && (verification.items[0]?.id || null) === firstId) {
          return { items, total: expectedTotal };
        }
        unstable = true;
        break;
      }
      if (page.items.length === 0) {
        unstable = true;
        break;
      }
    }

    if (!unstable) {
      throw new ApiError(
        "Task Agent 列表超过管理端单次加载上限，请缩小数据范围",
        400,
        "FRONTEND_LIST_LIMIT",
      );
    }
  }

  throw new ApiError(
    "Task Agent 列表在加载期间持续变化，请刷新后重试",
    409,
    "FRONTEND_UNSTABLE_PAGINATION",
  );
}

export const taskAgentApi = {
  context: () => apiRequest<AgentContext>(`${TASK_AGENT_PREFIX}/context`),
  definitions: {
    list: () => listAll<AgentDefinition>(`${TASK_AGENT_PREFIX}/definitions`),
    get: (definitionId: string) =>
      apiRequest<AgentDefinition>(resourcePath("definitions", definitionId)),
    create: (input: CreateAgentDefinitionInput) =>
      apiRequest<AgentDefinition>(`${TASK_AGENT_PREFIX}/definitions`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
  },
  versions: {
    list: (definitionId: string) =>
      listAll<AgentVersion>(`${resourcePath("definitions", definitionId)}/versions`),
    get: (versionId: string) =>
      apiRequest<AgentVersion>(resourcePath("versions", versionId)),
    publish: (definitionId: string, input: PublishAgentVersionInput) =>
      apiRequest<AgentVersion>(`${resourcePath("definitions", definitionId)}/versions`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
  },
  sessions: {
    list: () => listAll<AgentSession>(`${TASK_AGENT_PREFIX}/sessions`),
    get: (sessionId: string) =>
      apiRequest<AgentSession>(resourcePath("sessions", sessionId)),
    create: (input: CreateAgentSessionInput) =>
      apiRequest<AgentSession>(`${TASK_AGENT_PREFIX}/sessions`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    state: (sessionId: string, historyLimit = 100) =>
      apiRequest<AgentSessionState>(
        `${resourcePath("sessions", sessionId)}/state${toQuery({ history_limit: historyLimit })}`,
      ),
  },
  runs: {
    list: (sessionId: string) =>
      listAll<AgentRun>(`${resourcePath("sessions", sessionId)}/runs`),
    get: (runId: string) => apiRequest<AgentRun>(resourcePath("runs", runId)),
    create: (sessionId: string, input: CreateAgentRunInput) =>
      apiRequest<AgentRun>(`${resourcePath("sessions", sessionId)}/runs`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    transition: (runId: string, input: TransitionAgentRunInput) =>
      apiRequest<AgentRun>(`${resourcePath("runs", runId)}/transition`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
  },
  questions: {
    create: (runId: string, input: CreatePendingQuestionInput) =>
      apiRequest<PendingQuestion>(`${resourcePath("runs", runId)}/questions`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    overrideAnswer: (questionId: string, input: OverridePendingQuestionInput) =>
      apiRequest<QuestionAnswerResult>(
        `${resourcePath("questions", questionId)}/override-answer`,
        {
          method: "POST",
          body: JSON.stringify(input),
        },
      ),
  },
};
