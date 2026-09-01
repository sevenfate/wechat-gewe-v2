import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/api/tool-bridge", () => ({ toolBridgeApi: apiMocks }));
vi.mock("@/auth/session", () => ({
  authSession: { state: { user: { roles: ["owner"], permissions: [] } } },
}));

import ToolCallsView from "@/views/ToolCallsView.vue";

const call = {
  id: "call-1", workspace_id: "workspace-1", connector_deployment_id: "deployment-1",
  connector_revision_id: "revision-1", connector_activation_id: "activation-1",
  target_deployment_id: null, target_revision_id: null, target_activation_epoch: null,
  external_tool_call_id: "external-1", connector_context_digest: "digest",
  tool_name: "plugin.weather.query", tool_schema_version: "1.0", invocation_mode: "USER_REQUESTED",
  arguments: { city: "北京" }, arguments_sha256: "args", trace_id: "trace-1",
  actor_principal_id: null, bot_account_id: null, chatroom_id: null, contact_id: null,
  status: "SUCCEEDED", result: { text: "晴" }, error_code: null, error_detail: null,
  deadline_at: "2026-08-31T01:00:00Z", available_at: "2026-08-31T00:00:00Z",
  attempt_count: 1, started_at: "2026-08-31T00:00:01Z", finished_at: "2026-08-31T00:00:02Z",
  created_at: "2026-08-31T00:00:00Z", updated_at: "2026-08-31T00:00:02Z",
};

beforeEach(() => { apiMocks.list.mockResolvedValue({ items: [call], total: 1 }); });

describe("ToolCallsView", () => {
  it("loads read-only calls and opens details", async () => {
    const wrapper = mount(ToolCallsView);
    await flushPromises();
    expect(apiMocks.list).toHaveBeenCalledWith({ status: "", limit: 100 });
    expect(wrapper.text()).toContain("plugin.weather.query");
    await wrapper.get('button[aria-label="查看调用详情"]').trigger("click");
    expect(wrapper.get('[role="dialog"]').text()).toContain('"city": "北京"');
    expect(wrapper.get('[role="dialog"]').text()).toContain('"text": "晴"');
    expect(wrapper.findAll("button").some((button) => button.text().includes("执行"))).toBe(false);
  });

  it("passes the selected status as a read-only filter", async () => {
    const wrapper = mount(ToolCallsView);
    await flushPromises();
    await wrapper.get('select[aria-label="筛选 Tool 调用状态"]').setValue("DENIED");
    await flushPromises();
    expect(apiMocks.list).toHaveBeenLastCalledWith({ status: "DENIED", limit: 100 });
  });
});
