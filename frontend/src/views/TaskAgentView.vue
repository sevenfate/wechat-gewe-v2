<script setup lang="ts">
import {
  Activity,
  Bot,
  Braces,
  CheckCircle2,
  CircleHelp,
  Clock3,
  FilePlus2,
  History,
  MessageSquareReply,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Send,
  Square,
  X,
} from "lucide-vue-next";
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";

import { ApiError, createIdempotencyKey } from "@/api/client";
import {
  taskAgentApi,
  type AgentDefinition,
  type AgentEventType,
  type AgentInboxKind,
  type AgentRun,
  type AgentRunStatus,
  type AgentSession,
  type AgentSessionState,
  type AgentVersion,
  type PendingQuestion,
} from "@/api/task-agent";
import { authSession } from "@/auth/session";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { formatDateTime } from "@/utils/format";
import "@/styles/task-agent.css";

type DialogMode = "definition" | "version" | "session" | "run" | "question";
type ActivityTab = "events" | "questions" | "inbox";

const RUN_TRANSITIONS: Record<AgentRunStatus, readonly AgentRunStatus[]> = {
  QUEUED: ["RUNNING", "PAUSED", "FAILED", "CANCELLED", "EXPIRED"],
  RUNNING: [
    "QUEUED",
    "WAITING_APPROVAL",
    "WAITING_USER",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
  ],
  WAITING_APPROVAL: ["QUEUED", "PAUSED", "FAILED", "CANCELLED", "EXPIRED"],
  WAITING_USER: ["QUEUED", "FAILED", "CANCELLED", "EXPIRED"],
  PAUSED: ["QUEUED", "FAILED", "CANCELLED", "EXPIRED"],
  COMPLETED: [],
  FAILED: [],
  CANCELLED: [],
  EXPIRED: [],
};

const RUN_STATUS_LABELS: Record<AgentRunStatus, string> = {
  QUEUED: "排队",
  RUNNING: "运行中",
  WAITING_APPROVAL: "等待审批",
  WAITING_USER: "等待用户",
  PAUSED: "已暂停",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
  EXPIRED: "已过期",
};

const EVENT_LABELS: Record<AgentEventType, string> = {
  SESSION_CREATED: "Session 已创建",
  RUN_CREATED: "Run 已创建",
  RUN_STATUS_CHANGED: "运行状态已变化",
  QUESTION_ASKED: "已请求用户输入",
  QUESTION_ANSWERED: "用户已回答",
};

const INBOX_LABELS: Record<AgentInboxKind, string> = {
  RUN_REQUEST: "运行请求",
  QUESTION_ANSWER: "用户回答",
};

const isOwner = computed(() => authSession.state.user?.roles.includes("owner") === true);
const hasPermission = (permission: string) =>
  isOwner.value || authSession.state.user?.permissions.includes(permission) === true;
const canRead = computed(() => hasPermission("agent.read"));
const canWrite = computed(() => hasPermission("agent.write"));
const canRun = computed(() => hasPermission("agent.run"));
const canOverrideQuestion = computed(() => hasPermission("agent.question.override"));

const selectedWorkspaceId = ref("");
const selectedWorkspaceName = ref("");
const definitions = ref<AgentDefinition[]>([]);
const versions = ref<AgentVersion[]>([]);
const sessions = ref<AgentSession[]>([]);
const runs = ref<AgentRun[]>([]);
const sessionState = ref<AgentSessionState | null>(null);
const selectedDefinitionId = ref("");
const selectedVersionId = ref("");
const selectedSessionId = ref("");
const selectedRunId = ref("");
const activityTab = ref<ActivityTab>("events");
const loadingCatalog = ref(false);
const loadingState = ref(false);
const catalogError = ref<ApiError | null>(null);
const stateError = ref<ApiError | null>(null);
const actionError = ref<ApiError | null>(null);
const actionResult = ref("");
const submitting = ref(false);
const answeringQuestionId = ref("");
const dialog = ref<DialogMode | null>(null);
const answerDrafts = reactive<Record<string, string>>({});
const answerReasonDrafts = reactive<Record<string, string>>({});
const transitionTarget = ref<AgentRunStatus | "">("");
const transitionReason = ref("");
const transitionErrorCode = ref("");

const definitionDraft = reactive({ definitionKey: "", name: "", description: "" });
const versionDraft = reactive({
  specificationJson: JSON.stringify(
    { model: "", instructions: "", tools: [], limits: { max_steps: 20 } },
    null,
    2,
  ),
});
const sessionDraft = reactive({ title: "", scopeJson: "{}" });
const runDraft = reactive({ task: "", inputJson: "{}" });
const questionDraft = reactive({
  prompt: "",
  principalId: "",
  contextJson: "{}",
  expiresMinutes: 60,
});

let refreshTimer: ReturnType<typeof setInterval> | null = null;
let catalogRequestEpoch = 0;
let versionRequestEpoch = 0;
let sessionStateRequestEpoch = 0;

const selectedDefinition = computed(
  () => definitions.value.find((item) => item.id === selectedDefinitionId.value) || null,
);
const selectedVersion = computed(
  () => versions.value.find((item) => item.id === selectedVersionId.value) || null,
);
const selectedSession = computed(
  () => sessions.value.find((item) => item.id === selectedSessionId.value) || null,
);
const selectedRun = computed(() => {
  const chosen = runs.value.find((item) => item.id === selectedRunId.value);
  return chosen || sessionState.value?.active_run || runs.value[0] || null;
});
const visibleSessions = computed(() => {
  if (!selectedDefinitionId.value) return sessions.value;
  const versionIds = new Set(versions.value.map((item) => item.id));
  return sessions.value.filter((item) => versionIds.has(item.agent_version_id));
});
const selectedRunEvents = computed(() => {
  if (!selectedRun.value) return sessionState.value?.events || [];
  return (sessionState.value?.events || []).filter(
    (event) => event.run_id === null || event.run_id === selectedRun.value?.id,
  );
});
const selectedRunInbox = computed(() =>
  selectedRun.value
    ? (sessionState.value?.inbox || []).filter((item) => item.run_id === selectedRun.value?.id)
    : sessionState.value?.inbox || [],
);
const selectedRunQuestions = computed(() =>
  selectedRun.value
    ? (sessionState.value?.questions || []).filter(
        (item) => item.run_id === selectedRun.value?.id,
      )
    : sessionState.value?.questions || [],
);
const pendingQuestionCount = computed(
  () => selectedRunQuestions.value.filter((item) => item.status === "PENDING").length,
);
const activityHasMore = computed(() => {
  if (!sessionState.value) return false;
  if (activityTab.value === "events") return sessionState.value.events_has_more;
  if (activityTab.value === "questions") return sessionState.value.questions_has_more;
  return sessionState.value.inbox_has_more;
});
const transitionOptions = computed(() =>
  selectedRun.value ? RUN_TRANSITIONS[selectedRun.value.status] : [],
);
const canCreateRun = computed(
  () => canRun.value && selectedSession.value && !sessionState.value?.active_run,
);
const canAskQuestion = computed(
  () => canRun.value && selectedRun.value?.status === "RUNNING",
);

function toApiError(caught: unknown, fallback: string): ApiError {
  return caught instanceof ApiError ? caught : new ApiError(fallback, 0);
}

function parseObject(value: string, label: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${label}不是有效的 JSON`);
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  return parsed as Record<string, unknown>;
}

function compactId(value?: string | null): string {
  if (!value) return "-";
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function sessionTitle(session: AgentSession): string {
  for (const key of ["title", "name", "objective", "task"]) {
    const value = session.task_scope[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return `Session ${compactId(session.id)}`;
}

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function eventLabel(eventType: AgentEventType): string {
  return EVENT_LABELS[eventType] || eventType;
}

function inboxLabel(kind: AgentInboxKind): string {
  return INBOX_LABELS[kind] || kind;
}

function runStatusLabel(status: AgentRunStatus): string {
  return RUN_STATUS_LABELS[status] || status;
}

async function initializeWorkspace() {
  const requestEpoch = ++catalogRequestEpoch;
  loadingCatalog.value = true;
  catalogError.value = null;
  try {
    const context = await taskAgentApi.context();
    if (requestEpoch !== catalogRequestEpoch) return;
    selectedWorkspaceId.value = context.workspace_id;
    selectedWorkspaceName.value = context.workspace_name;
    await loadWorkspaceDataForEpoch(requestEpoch);
  } catch (caught) {
    if (requestEpoch === catalogRequestEpoch) {
      catalogError.value = toApiError(caught, "读取 Task Agent 工作区失败");
    }
  } finally {
    if (requestEpoch === catalogRequestEpoch) loadingCatalog.value = false;
  }
}

async function loadVersions(definitionId = selectedDefinitionId.value) {
  const requestEpoch = ++versionRequestEpoch;
  if (!definitionId) {
    versions.value = [];
    selectedVersionId.value = "";
    return;
  }
  const result = await taskAgentApi.versions.list(definitionId);
  if (requestEpoch !== versionRequestEpoch || definitionId !== selectedDefinitionId.value) return;
  versions.value = [...result.items].sort(
    (left, right) => right.version_number - left.version_number,
  );
  if (!versions.value.some((item) => item.id === selectedVersionId.value)) {
    selectedVersionId.value = versions.value[0]?.id || "";
  }
}

async function loadWorkspaceData() {
  if (!canRead.value || !selectedWorkspaceId.value) return;
  ++sessionStateRequestEpoch;
  loadingState.value = false;
  await loadWorkspaceDataForEpoch(++catalogRequestEpoch);
}

async function loadWorkspaceDataForEpoch(requestEpoch: number) {
  loadingCatalog.value = true;
  catalogError.value = null;
  actionResult.value = "";
  try {
    const [definitionResult, sessionResult] = await Promise.all([
      taskAgentApi.definitions.list(),
      taskAgentApi.sessions.list(),
    ]);
    if (requestEpoch !== catalogRequestEpoch) return;
    definitions.value = [...definitionResult.items].sort((left, right) =>
      right.updated_at.localeCompare(left.updated_at),
    );
    sessions.value = [...sessionResult.items].sort((left, right) =>
      right.created_at.localeCompare(left.created_at),
    );
    if (!definitions.value.some((item) => item.id === selectedDefinitionId.value)) {
      selectedDefinitionId.value = definitions.value[0]?.id || "";
    }
    await loadVersions();
    if (requestEpoch !== catalogRequestEpoch) return;
    if (!visibleSessions.value.some((item) => item.id === selectedSessionId.value)) {
      selectedSessionId.value = visibleSessions.value[0]?.id || "";
    }
    if (selectedSessionId.value) await loadSessionState();
    else {
      runs.value = [];
      sessionState.value = null;
      loadingState.value = false;
      stateError.value = null;
    }
  } catch (caught) {
    if (requestEpoch === catalogRequestEpoch) {
      catalogError.value = toApiError(caught, "读取 Task Agent 工作区失败");
    }
  } finally {
    if (requestEpoch === catalogRequestEpoch) loadingCatalog.value = false;
  }
}

async function selectDefinition(definitionId: string) {
  const requestEpoch = ++catalogRequestEpoch;
  ++sessionStateRequestEpoch;
  loadingState.value = false;
  selectedDefinitionId.value = definitionId;
  selectedVersionId.value = "";
  selectedSessionId.value = "";
  selectedRunId.value = "";
  sessionState.value = null;
  stateError.value = null;
  loadingCatalog.value = true;
  catalogError.value = null;
  try {
    await loadVersions(definitionId);
    if (requestEpoch !== catalogRequestEpoch || definitionId !== selectedDefinitionId.value) return;
    selectedSessionId.value = visibleSessions.value[0]?.id || "";
    if (selectedSessionId.value) await loadSessionState();
  } catch (caught) {
    if (requestEpoch === catalogRequestEpoch) {
      catalogError.value = toApiError(caught, "读取 Agent 版本失败");
    }
  } finally {
    if (requestEpoch === catalogRequestEpoch) loadingCatalog.value = false;
  }
}

async function selectSession(sessionId: string) {
  ++sessionStateRequestEpoch;
  loadingState.value = false;
  selectedSessionId.value = sessionId;
  selectedRunId.value = "";
  sessionState.value = null;
  await loadSessionState();
}

async function loadSessionState(silent = false) {
  const sessionId = selectedSessionId.value;
  if (!sessionId) return;
  const requestEpoch = ++sessionStateRequestEpoch;
  if (!silent) {
    loadingState.value = true;
    stateError.value = null;
  }
  try {
    const [nextState, runResult] = await Promise.all([
      taskAgentApi.sessions.state(sessionId),
      taskAgentApi.runs.list(sessionId),
    ]);
    if (requestEpoch !== sessionStateRequestEpoch || sessionId !== selectedSessionId.value) return;
    sessionState.value = nextState;
    stateError.value = null;
    runs.value = [...runResult.items].sort((left, right) =>
      right.created_at.localeCompare(left.created_at),
    );
    const currentSelectionExists = runs.value.some((item) => item.id === selectedRunId.value);
    if (!currentSelectionExists) {
      selectedRunId.value = nextState.active_run?.id || runs.value[0]?.id || "";
    }
    const sessionIndex = sessions.value.findIndex((item) => item.id === nextState.session.id);
    if (sessionIndex >= 0) sessions.value[sessionIndex] = nextState.session;
  } catch (caught) {
    if (!silent && requestEpoch === sessionStateRequestEpoch) {
      stateError.value = toApiError(caught, "读取 Agent Session 状态失败");
    }
  } finally {
    if (!silent && requestEpoch === sessionStateRequestEpoch) loadingState.value = false;
  }
}

function openDialog(mode: DialogMode) {
  dialog.value = mode;
  actionError.value = null;
  if (mode === "definition") {
    Object.assign(definitionDraft, { definitionKey: "", name: "", description: "" });
  } else if (mode === "version") {
    Object.assign(versionDraft, {
      specificationJson: JSON.stringify(
        { model: "", instructions: "", tools: [], limits: { max_steps: 20 } },
        null,
        2,
      ),
    });
  } else if (mode === "session") {
    Object.assign(sessionDraft, { title: "", scopeJson: "{}" });
  } else if (mode === "run") {
    Object.assign(runDraft, { task: "", inputJson: "{}" });
  } else if (mode === "question") {
    Object.assign(questionDraft, {
      prompt: "",
      principalId: sessionState.value?.session.requester_principal_id || "",
      contextJson: "{}",
      expiresMinutes: 60,
    });
  }
}

function closeDialog() {
  if (submitting.value) return;
  dialog.value = null;
  actionError.value = null;
}

async function submitDialog() {
  const mode = dialog.value;
  if (!mode) return;
  submitting.value = true;
  actionError.value = null;
  actionResult.value = "";
  try {
    if (mode === "definition") {
      const created = await taskAgentApi.definitions.create({
        workspace_id: selectedWorkspaceId.value,
        definition_key: definitionDraft.definitionKey.trim(),
        name: definitionDraft.name.trim(),
        description: definitionDraft.description.trim(),
      });
      definitions.value = [created, ...definitions.value.filter((item) => item.id !== created.id)];
      selectedDefinitionId.value = created.id;
      versions.value = [];
      selectedVersionId.value = "";
      selectedSessionId.value = "";
      sessionState.value = null;
      actionResult.value = `已创建 Agent 定义「${created.name}」`;
    } else if (mode === "version") {
      if (!selectedDefinition.value) throw new Error("请先选择 Agent 定义");
      const created = await taskAgentApi.versions.publish(selectedDefinition.value.id, {
        specification: parseObject(versionDraft.specificationJson, "Specification"),
      });
      versions.value = [created, ...versions.value.filter((item) => item.id !== created.id)].sort(
        (left, right) => right.version_number - left.version_number,
      );
      selectedVersionId.value = created.id;
      actionResult.value = `已发布 v${created.version_number}`;
    } else if (mode === "session") {
      if (!selectedVersion.value) throw new Error("请先选择 Agent 版本");
      const scope = parseObject(sessionDraft.scopeJson, "Task Scope");
      if (sessionDraft.title.trim()) scope.title = sessionDraft.title.trim();
      const created = await taskAgentApi.sessions.create({
        workspace_id: selectedWorkspaceId.value,
        agent_version_id: selectedVersion.value.id,
        task_scope: scope,
      });
      sessions.value = [created, ...sessions.value.filter((item) => item.id !== created.id)];
      selectedSessionId.value = created.id;
      selectedRunId.value = "";
      actionResult.value = "Session 已创建";
      await loadSessionState();
    } else if (mode === "run") {
      if (!selectedSession.value) throw new Error("请先选择 Session");
      const payload = parseObject(runDraft.inputJson, "附加输入");
      payload.task = runDraft.task.trim();
      const created = await taskAgentApi.runs.create(selectedSession.value.id, {
        idempotency_key: createIdempotencyKey(),
        input_payload: payload,
      });
      selectedRunId.value = created.id;
      actionResult.value = "Run 已进入队列";
      await loadSessionState();
    } else if (mode === "question") {
      if (!selectedRun.value) throw new Error("请先选择 Run");
      const expiresAt = new Date(
        Date.now() + Math.max(1, questionDraft.expiresMinutes) * 60_000,
      );
      await taskAgentApi.questions.create(selectedRun.value.id, {
        allowed_principal_id: questionDraft.principalId.trim(),
        prompt: questionDraft.prompt.trim(),
        context: parseObject(questionDraft.contextJson, "问题 Context"),
        expires_at: expiresAt.toISOString(),
      });
      activityTab.value = "questions";
      actionResult.value = "问题已登记，Run 正在等待用户回答";
      await loadSessionState();
    }
    dialog.value = null;
  } catch (caught) {
    actionError.value = toApiError(
      caught,
      caught instanceof Error ? caught.message : "Task Agent 操作失败",
    );
  } finally {
    submitting.value = false;
  }
}

async function submitTransition() {
  const run = selectedRun.value;
  const target = transitionTarget.value;
  if (!run || !target) return;
  submitting.value = true;
  actionError.value = null;
  actionResult.value = "";
  try {
    await taskAgentApi.runs.transition(run.id, {
      status: target,
      reason: transitionReason.value.trim() || null,
      error_code: target === "FAILED" ? transitionErrorCode.value.trim() || null : null,
    });
    actionResult.value = `Run 已切换为${runStatusLabel(target)}`;
    await loadSessionState();
  } catch (caught) {
    actionError.value = toApiError(caught, "更新 Run 状态失败");
  } finally {
    submitting.value = false;
  }
}

function answerPayload(raw: string): Record<string, unknown> {
  const trimmed = raw.trim();
  if (trimmed.startsWith("{")) return parseObject(trimmed, "回答");
  return { text: trimmed };
}

async function overrideQuestion(question: PendingQuestion) {
  const raw = answerDrafts[question.id]?.trim() || "";
  const reason = answerReasonDrafts[question.id]?.trim() || "";
  if (!raw || !reason) return;
  answeringQuestionId.value = question.id;
  actionError.value = null;
  actionResult.value = "";
  try {
    await taskAgentApi.questions.overrideAnswer(question.id, {
      answer_payload: answerPayload(raw),
      reason,
    });
    answerDrafts[question.id] = "";
    answerReasonDrafts[question.id] = "";
    actionResult.value = "管理员代答已审计，Run 已重新排队";
    await loadSessionState();
  } catch (caught) {
    actionError.value = toApiError(
      caught,
      caught instanceof Error ? caught.message : "提交管理员代答失败",
    );
  } finally {
    answeringQuestionId.value = "";
  }
}

watch(
  () => selectedRun.value?.status,
  () => {
    transitionTarget.value = transitionOptions.value[0] || "";
    transitionReason.value = "";
    transitionErrorCode.value = "";
  },
  { immediate: true },
);

watch(transitionTarget, (target) => {
  if (target !== "FAILED") transitionErrorCode.value = "";
});

onMounted(async () => {
  if (!canRead.value) return;
  await initializeWorkspace();
  refreshTimer = setInterval(() => {
    if (
      document.visibilityState === "visible" &&
      selectedSessionId.value &&
      !loadingState.value &&
      !submitting.value &&
      !answeringQuestionId.value
    ) {
      void loadSessionState(true);
    }
  }, 8_000);
});

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer);
  ++catalogRequestEpoch;
  ++versionRequestEpoch;
  ++sessionStateRequestEpoch;
});
</script>

<template>
  <div class="page-stack task-agent-page">
    <PageHeader title="任务 Agent" description="定义、运行与跟踪可恢复的多步骤任务">
      <template #actions>
        <span v-if="canRead && selectedWorkspaceName" class="agent-workspace-name">
          {{ selectedWorkspaceName }}
        </span>
      </template>
    </PageHeader>

    <EmptyState
      v-if="!canRead"
      title="没有 Task Agent 读取权限"
      detail="需要 agent.read capability"
    >
      <template #icon><Bot :size="23" /></template>
    </EmptyState>

    <template v-else>
      <div v-if="actionResult" class="agent-result-band" role="status">
        <CheckCircle2 :size="16" />
        <span>{{ actionResult }}</span>
      </div>
      <div v-if="actionError && !dialog" class="inline-error" role="alert">
        {{ actionError.message }}
        <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
      </div>

      <ErrorState
        v-if="catalogError && !selectedWorkspaceId"
        :error="catalogError"
        @retry="initializeWorkspace"
      />

      <section v-else-if="!selectedWorkspaceId" class="data-panel">
        <EmptyState title="尚未初始化工作区">
          <template #icon><Bot :size="23" /></template>
        </EmptyState>
      </section>

      <template v-else>
        <section class="agent-definition-strip">
          <div class="agent-definition-selectors">
            <label class="agent-select-control">
              <span>Agent 定义</span>
              <select
                :value="selectedDefinitionId"
                :disabled="loadingCatalog || !definitions.length"
                @change="selectDefinition(($event.target as HTMLSelectElement).value)"
              >
                <option v-if="!definitions.length" value="">暂无定义</option>
                <option
                  v-for="definition in definitions"
                  :key="definition.id"
                  :value="definition.id"
                >
                  {{ definition.name }} · {{ definition.definition_key }}
                </option>
              </select>
            </label>
            <label class="agent-select-control agent-select-control--version">
              <span>版本</span>
              <select v-model="selectedVersionId" :disabled="loadingCatalog || !versions.length">
                <option v-if="!versions.length" value="">尚未发布</option>
                <option v-for="version in versions" :key="version.id" :value="version.id">
                  v{{ version.version_number }} · {{ formatDateTime(version.published_at) }}
                </option>
              </select>
            </label>
          </div>

          <div v-if="selectedDefinition" class="agent-definition-summary">
            <strong>{{ selectedDefinition.name }}</strong>
            <span>{{ selectedDefinition.description || "未填写说明" }}</span>
            <code>{{ selectedDefinition.definition_key }}</code>
          </div>
          <div class="agent-definition-actions">
            <button
              class="icon-button"
              type="button"
              :disabled="loadingCatalog"
              title="刷新工作台"
              aria-label="刷新工作台"
              @click="loadWorkspaceData"
            >
              <RefreshCw :class="{ spin: loadingCatalog }" :size="17" />
            </button>
            <button
              v-if="canWrite"
              class="button button--secondary"
              type="button"
              @click="openDialog('definition')"
            >
              <Plus :size="15" />新建定义
            </button>
            <button
              v-if="canWrite && selectedDefinition"
              class="button button--primary"
              type="button"
              @click="openDialog('version')"
            >
              <FilePlus2 :size="15" />发布版本
            </button>
          </div>
        </section>

        <ErrorState v-if="catalogError" :error="catalogError" @retry="loadWorkspaceData" />
        <LoadingState v-else-if="loadingCatalog && !definitions.length && !sessions.length" />

        <div v-else class="agent-workbench">
          <aside class="agent-session-panel">
            <header class="agent-panel-header">
              <div>
                <span class="agent-panel-eyebrow">SESSIONS</span>
                <h3>任务会话</h3>
              </div>
              <button
                v-if="canRun && selectedVersion"
                class="icon-button icon-button--small"
                type="button"
                title="新建 Session"
                aria-label="新建 Session"
                @click="openDialog('session')"
              >
                <Plus :size="16" />
              </button>
            </header>

            <EmptyState
              v-if="!selectedDefinition"
              title="暂无 Agent 定义"
              detail="创建定义并发布版本后即可发起任务"
            >
              <template #icon><Bot :size="22" /></template>
            </EmptyState>
            <EmptyState
              v-else-if="!versions.length"
              title="尚未发布版本"
              detail="Session 必须绑定不可变的 Agent 版本"
            >
              <template #icon><History :size="22" /></template>
            </EmptyState>
            <EmptyState v-else-if="!visibleSessions.length" title="暂无 Session">
              <template #icon><Activity :size="22" /></template>
              <template v-if="canRun" #action>
                <button class="button button--primary" type="button" @click="openDialog('session')">
                  <Plus :size="15" />新建 Session
                </button>
              </template>
            </EmptyState>
            <div v-else class="agent-session-list" role="listbox" aria-label="任务 Session">
              <button
                v-for="session in visibleSessions"
                :key="session.id"
                class="agent-session-item"
                :class="{ 'agent-session-item--active': session.id === selectedSessionId }"
                type="button"
                role="option"
                :aria-selected="session.id === selectedSessionId"
                @click="selectSession(session.id)"
              >
                <span class="agent-session-item-icon"><Activity :size="16" /></span>
                <span class="agent-session-item-copy">
                  <strong>{{ sessionTitle(session) }}</strong>
                  <small>{{ formatDateTime(session.updated_at) }} · {{ compactId(session.id) }}</small>
                </span>
                <span class="agent-session-seq">#{{ session.last_event_seq }}</span>
              </button>
            </div>
          </aside>

          <section class="agent-run-panel">
            <EmptyState v-if="!selectedSession" title="选择一个 Session">
              <template #icon><Activity :size="23" /></template>
            </EmptyState>
            <template v-else>
              <header class="agent-run-header">
                <div class="agent-run-heading">
                  <span class="agent-panel-eyebrow">CURRENT SESSION</span>
                  <h3>{{ sessionTitle(selectedSession) }}</h3>
                  <code>{{ selectedSession.id }}</code>
                </div>
                <div class="agent-run-header-actions">
                  <label v-if="runs.length" class="agent-run-select">
                    <span>运行历史</span>
                    <select v-model="selectedRunId">
                      <option v-for="run in runs" :key="run.id" :value="run.id">
                        {{ runStatusLabel(run.status) }} · {{ formatDateTime(run.created_at) }}
                      </option>
                    </select>
                  </label>
                  <button
                    class="icon-button"
                    type="button"
                    :disabled="loadingState"
                    title="刷新 Session"
                    aria-label="刷新 Session"
                    @click="loadSessionState()"
                  >
                    <RefreshCw :class="{ spin: loadingState }" :size="17" />
                  </button>
                  <button
                    v-if="canCreateRun"
                    class="button button--primary"
                    type="button"
                    @click="openDialog('run')"
                  >
                    <Send :size="15" />发起 Run
                  </button>
                </div>
              </header>

              <ErrorState v-if="stateError" :error="stateError" @retry="loadSessionState()" />
              <LoadingState v-else-if="loadingState && !sessionState" />
              <template v-else-if="sessionState">
                <div v-if="selectedRun" class="agent-run-summary">
                  <div class="agent-run-status">
                    <StatusBadge :status="selectedRun.status" />
                    <div>
                      <strong>Run {{ compactId(selectedRun.id) }}</strong>
                      <small>
                        创建于 {{ formatDateTime(selectedRun.created_at) }}
                        <template v-if="selectedRun.started_at">
                          · 启动于 {{ formatDateTime(selectedRun.started_at) }}
                        </template>
                      </small>
                    </div>
                  </div>
                  <dl class="agent-run-facts">
                    <div>
                      <dt>Inbox</dt>
                      <dd>{{ selectedSession.last_inbox_seq }}</dd>
                    </div>
                    <div>
                      <dt>Events</dt>
                      <dd>{{ selectedSession.last_event_seq }}</dd>
                    </div>
                    <div>
                      <dt>错误码</dt>
                      <dd>{{ selectedRun.last_error_code || "-" }}</dd>
                    </div>
                  </dl>
                </div>

                <form
                  v-if="canRun && selectedRun && transitionOptions.length"
                  class="agent-transition-bar"
                  @submit.prevent="submitTransition"
                >
                  <label>
                    <span>状态操作</span>
                    <select v-model="transitionTarget" required>
                      <option v-for="status in transitionOptions" :key="status" :value="status">
                        {{ runStatusLabel(status) }}
                      </option>
                    </select>
                  </label>
                  <label class="agent-transition-reason">
                    <span>原因</span>
                    <input v-model="transitionReason" type="text" maxlength="500" placeholder="可选" />
                  </label>
                  <label v-if="transitionTarget === 'FAILED'" class="agent-transition-error">
                    <span>错误码</span>
                    <input
                      v-model="transitionErrorCode"
                      type="text"
                      maxlength="100"
                      placeholder="AGENT_ERROR"
                    />
                  </label>
                  <button class="button button--secondary" type="submit" :disabled="submitting">
                    <Pause v-if="transitionTarget === 'PAUSED'" :size="15" />
                    <Square v-else-if="transitionTarget === 'CANCELLED'" :size="14" />
                    <Play v-else :size="15" />
                    执行
                  </button>
                  <button
                    v-if="canAskQuestion"
                    class="button button--secondary"
                    type="button"
                    @click="openDialog('question')"
                  >
                    <CircleHelp :size="15" />请求输入
                  </button>
                </form>

                <div v-if="!selectedRun" class="agent-no-run">
                  <EmptyState title="此 Session 尚无 Run">
                    <template #icon><Play :size="22" /></template>
                    <template v-if="canCreateRun" #action>
                      <button class="button button--primary" type="button" @click="openDialog('run')">
                        <Send :size="15" />发起 Run
                      </button>
                    </template>
                  </EmptyState>
                </div>

                <section v-else class="agent-activity">
                  <header class="agent-activity-header">
                    <div class="agent-activity-tabs" role="tablist" aria-label="Session 记录">
                      <button
                        type="button"
                        role="tab"
                        :aria-selected="activityTab === 'events'"
                        :class="{ active: activityTab === 'events' }"
                        @click="activityTab = 'events'"
                      >
                        <History :size="14" />事件 {{ selectedRunEvents.length }}
                      </button>
                      <button
                        type="button"
                        role="tab"
                        :aria-selected="activityTab === 'questions'"
                        :class="{ active: activityTab === 'questions' }"
                        @click="activityTab = 'questions'"
                      >
                        <CircleHelp :size="14" />问题 {{ selectedRunQuestions.length }}
                        <span v-if="pendingQuestionCount" class="agent-tab-count">
                          {{ pendingQuestionCount }}
                        </span>
                      </button>
                      <button
                        type="button"
                        role="tab"
                        :aria-selected="activityTab === 'inbox'"
                        :class="{ active: activityTab === 'inbox' }"
                        @click="activityTab = 'inbox'"
                      >
                        <Braces :size="14" />输入 {{ selectedRunInbox.length }}
                      </button>
                    </div>
                  </header>

                  <div v-if="activityHasMore" class="agent-history-notice" role="status">
                    当前显示最近 100 条记录
                  </div>

                  <div v-if="activityTab === 'events'" class="agent-event-list">
                    <EmptyState v-if="!selectedRunEvents.length" title="暂无事件">
                      <template #icon><History :size="22" /></template>
                    </EmptyState>
                    <article
                      v-for="event in [...selectedRunEvents].reverse()"
                      v-else
                      :key="event.id"
                      class="agent-event-row"
                    >
                      <span class="agent-event-seq">{{ event.seq }}</span>
                      <span class="agent-event-marker" />
                      <div class="agent-event-copy">
                        <div>
                          <strong>{{ eventLabel(event.event_type) }}</strong>
                          <time>{{ formatDateTime(event.created_at) }}</time>
                        </div>
                        <details>
                          <summary>查看 payload</summary>
                          <pre>{{ prettyJson(event.payload) }}</pre>
                        </details>
                      </div>
                    </article>
                  </div>

                  <div v-else-if="activityTab === 'questions'" class="agent-question-list">
                    <EmptyState v-if="!selectedRunQuestions.length" title="暂无用户问题">
                      <template #icon><CircleHelp :size="22" /></template>
                    </EmptyState>
                    <article
                      v-for="question in [...selectedRunQuestions].reverse()"
                      v-else
                      :key="question.id"
                      class="agent-question-row"
                    >
                      <header>
                        <div>
                          <strong>{{ question.prompt }}</strong>
                          <small>
                            {{ compactId(question.allowed_principal_id) }} · 截止
                            {{ formatDateTime(question.expires_at) }}
                          </small>
                        </div>
                        <StatusBadge :status="question.status" />
                      </header>
                      <form
                        v-if="canOverrideQuestion && question.status === 'PENDING'"
                        class="agent-answer-form"
                        @submit.prevent="overrideQuestion(question)"
                      >
                        <textarea
                          v-model="answerDrafts[question.id]"
                          rows="2"
                          required
                          placeholder="管理员代答内容，或填写 JSON 对象"
                        />
                        <input
                          v-model="answerReasonDrafts[question.id]"
                          type="text"
                          maxlength="500"
                          required
                          placeholder="代答原因（会写入审计）"
                        />
                        <button
                          class="button button--primary"
                          type="submit"
                          :disabled="answeringQuestionId === question.id"
                        >
                          <MessageSquareReply :size="15" />
                          {{ answeringQuestionId === question.id ? "提交中" : "管理员代答" }}
                        </button>
                      </form>
                      <details v-else-if="question.answer_payload">
                        <summary>查看回答</summary>
                        <pre>{{ prettyJson(question.answer_payload) }}</pre>
                      </details>
                    </article>
                  </div>

                  <div v-else class="agent-inbox-list">
                    <EmptyState v-if="!selectedRunInbox.length" title="暂无输入记录">
                      <template #icon><Braces :size="22" /></template>
                    </EmptyState>
                    <article
                      v-for="item in [...selectedRunInbox].reverse()"
                      v-else
                      :key="item.id"
                      class="agent-inbox-row"
                    >
                      <span class="agent-inbox-kind">{{ inboxLabel(item.kind) }}</span>
                      <div>
                        <strong>#{{ item.seq }} · {{ compactId(item.actor_principal_id) }}</strong>
                        <time>{{ formatDateTime(item.created_at) }}</time>
                      </div>
                      <details>
                        <summary>查看 payload</summary>
                        <pre>{{ prettyJson(item.payload) }}</pre>
                      </details>
                    </article>
                  </div>
                </section>
              </template>
            </template>
          </section>
        </div>
      </template>
    </template>

    <div v-if="dialog" class="operation-dialog-backdrop" @click.self="closeDialog">
      <form
        class="operation-dialog operation-dialog--wide agent-dialog"
        role="dialog"
        aria-modal="true"
        @submit.prevent="submitDialog"
      >
        <header class="operation-dialog-header">
          <h3>
            {{
              dialog === "definition"
                ? "新建 Agent 定义"
                : dialog === "version"
                  ? "发布 Agent 版本"
                  : dialog === "session"
                    ? "新建 Session"
                    : dialog === "run"
                      ? "发起 Run"
                      : "请求用户输入"
            }}
          </h3>
          <button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="closeDialog">
            <X :size="18" />
          </button>
        </header>

        <div class="operation-dialog-body">
          <div v-if="dialog === 'definition'" class="agent-dialog-grid">
            <label class="field-control">
              <span>Definition Key</span>
              <input
                v-model="definitionDraft.definitionKey"
                type="text"
                maxlength="120"
                placeholder="research-assistant"
                required
              />
            </label>
            <label class="field-control">
              <span>名称</span>
              <input v-model="definitionDraft.name" type="text" maxlength="120" required />
            </label>
            <label class="field-control field-control--wide">
              <span>说明</span>
              <textarea v-model="definitionDraft.description" rows="3" maxlength="1000" />
            </label>
          </div>

          <div v-else-if="dialog === 'version'" class="agent-dialog-grid">
            <label class="field-control field-control--wide agent-json-field">
              <span>Specification JSON</span>
              <textarea
                v-model="versionDraft.specificationJson"
                rows="13"
                spellcheck="false"
                required
              />
            </label>
          </div>

          <div v-else-if="dialog === 'session'" class="agent-dialog-grid">
            <label class="field-control field-control--wide">
              <span>Session 名称</span>
              <input v-model="sessionDraft.title" type="text" maxlength="160" />
            </label>
            <label class="field-control field-control--wide agent-json-field">
              <span>Task Scope JSON</span>
              <textarea v-model="sessionDraft.scopeJson" rows="7" spellcheck="false" required />
            </label>
          </div>

          <div v-else-if="dialog === 'run'" class="agent-dialog-grid">
            <label class="field-control field-control--wide">
              <span>任务内容</span>
              <textarea v-model="runDraft.task" rows="5" maxlength="12000" required />
            </label>
            <label class="field-control field-control--wide agent-json-field">
              <span>附加输入 JSON</span>
              <textarea v-model="runDraft.inputJson" rows="6" spellcheck="false" required />
            </label>
          </div>

          <div v-else class="agent-dialog-grid">
            <label class="field-control field-control--wide">
              <span>问题</span>
              <textarea v-model="questionDraft.prompt" rows="4" maxlength="4000" required />
            </label>
            <label class="field-control">
              <span>允许回答的 Principal ID</span>
              <input v-model="questionDraft.principalId" type="text" required />
            </label>
            <label class="field-control">
              <span>有效分钟数</span>
              <input v-model.number="questionDraft.expiresMinutes" type="number" min="1" max="10080" required />
            </label>
            <label class="field-control field-control--wide agent-json-field">
              <span>Context JSON</span>
              <textarea v-model="questionDraft.contextJson" rows="6" spellcheck="false" required />
            </label>
          </div>

          <div v-if="actionError" class="inline-error" role="alert">
            {{ actionError.message }}
            <code v-if="actionError.traceId">Trace {{ actionError.traceId }}</code>
          </div>
        </div>

        <footer class="operation-dialog-actions">
          <button class="button button--secondary" type="button" :disabled="submitting" @click="closeDialog">
            取消
          </button>
          <button class="button button--primary" type="submit" :disabled="submitting">
            {{ submitting ? "正在提交" : dialog === "run" ? "发起" : "确认" }}
          </button>
        </footer>
      </form>
    </div>
  </div>
</template>
