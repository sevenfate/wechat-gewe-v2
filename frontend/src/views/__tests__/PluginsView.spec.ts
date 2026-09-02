import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  activate: vi.fn(),
  catalog: vi.fn(),
  context: vi.fn(),
  createDeployment: vi.fn(),
  createRevision: vi.fn(),
  deactivate: vi.fn(),
  health: vi.fn(),
  installBuiltin: vi.fn(),
  revisionDraft: vi.fn(),
}));
vi.mock("@/api/plugin-management", () => ({ pluginManagementApi: apiMocks }));
vi.mock("@/auth/session", () => ({
  authSession: {
    state: {
      user: { roles: ["owner"], permissions: [] },
    },
  },
}));

import PluginsView from "@/views/PluginsView.vue";

const marker = "__WECHAT_BOT_SECRET_RETAINED__";
const plugin = {
  id: "plugin-record",
  workspace_id: "workspace-1",
  plugin_id: "builtin.weather",
  name: "天气查询",
  description: "天气命令与查询工具",
  retired_at: null,
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};
const legacyPackage = {
  id: "package-1",
  plugin_id: plugin.id,
  semantic_version: "0.1.0",
  package_sha256: "legacy-package-sha",
  manifest: {
    id: plugin.plugin_id,
    name: plugin.name,
    version: "0.1.0",
    capabilities: ["message.reply.legacy"],
  },
  status: "AVAILABLE",
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};
const latestPackage = {
  ...legacyPackage,
  id: "package-2",
  semantic_version: "0.2.0",
  package_sha256: "latest-package-sha",
  manifest: {
    ...legacyPackage.manifest,
    version: "0.2.0",
    capabilities: ["message.forward.current"],
  },
  created_at: "2026-08-30T01:00:00Z",
  updated_at: "2026-08-30T01:00:00Z",
};
const deployment = {
  id: "deployment-1",
  workspace_id: plugin.workspace_id,
  plugin_id: plugin.id,
  name: "天气生产配置",
  status: "RUNNING",
  active_revision_id: "revision-2",
  last_error: null,
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
};
const revisions = [
  {
    id: "revision-2",
    deployment_id: deployment.id,
    package_version_id: latestPackage.id,
    revision_number: 2,
    config_fingerprint: "fingerprint-2",
    scope: { workspace_id: plugin.workspace_id, chatroom_ids: ["group-2"] },
    grants: ["message.forward.current"],
    content_sha256: "revision-two-sha",
    created_at: "2026-08-30T01:00:00Z",
    updated_at: "2026-08-30T01:00:00Z",
  },
  {
    id: "revision-1",
    deployment_id: deployment.id,
    package_version_id: legacyPackage.id,
    revision_number: 1,
    config_fingerprint: "fingerprint-1",
    scope: { workspace_id: plugin.workspace_id, chatroom_ids: ["group-1"] },
    grants: ["message.reply.legacy"],
    content_sha256: "revision-one-sha",
    created_at: "2026-08-30T00:00:00Z",
    updated_at: "2026-08-30T00:00:00Z",
  },
];

function draftFor(revisionId: string) {
  const revision = revisions.find((item) => item.id === revisionId)!;
  return {
    source_revision_id: revision.id,
    package_version_id: revision.package_version_id,
    config: {
      endpoint_url: `https://weather.test/${revision.revision_number}`,
      api_key: marker,
      client_uuid: "stable-client",
    },
    scope: revision.scope,
    grants: revision.grants,
    secret_placeholder: marker,
  };
}

async function mountLoadedView() {
  const wrapper = mount(PluginsView);
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  apiMocks.catalog.mockResolvedValue({
    plugins: [plugin],
    packages: [legacyPackage, latestPackage],
    deployments: [deployment],
    revisions,
  });
  apiMocks.context.mockResolvedValue({ workspace_id: plugin.workspace_id, name: "Primary" });
  apiMocks.revisionDraft.mockImplementation(
    async (_deploymentId: string, revisionId: string) => draftFor(revisionId),
  );
  apiMocks.createRevision.mockResolvedValue({
    ...revisions[0],
    id: "revision-3",
    revision_number: 3,
  });
  apiMocks.activate.mockResolvedValue({ deployment });
});

describe("PluginsView revision management", () => {
  it("loads and reloads the selected draft without replacing the marker", async () => {
    const wrapper = await mountLoadedView();
    expect(wrapper.find('button[aria-label="启动或回滚"]').exists()).toBe(true);

    await wrapper.get('button[aria-label="升级配置"]').trigger("click");
    await flushPromises();

    expect(apiMocks.revisionDraft).toHaveBeenCalledWith(deployment.id, "revision-2");
    const source = wrapper.get<HTMLSelectElement>('select[aria-label="来源 Revision"]');
    const packageSelect = wrapper.get<HTMLSelectElement>('select[aria-label="插件包"]');
    expect(source.element.value).toBe("revision-2");
    expect(packageSelect.element.value).toBe(latestPackage.id);
    expect(wrapper.get("fieldset").text()).toContain("message.forward.current");
    expect(wrapper.findAll("textarea")[0].element.value).toContain(marker);
    expect(wrapper.findAll("textarea")[0].element.value).toContain("/2");

    await source.setValue("revision-1");
    await flushPromises();

    expect(apiMocks.revisionDraft).toHaveBeenLastCalledWith(deployment.id, "revision-1");
    expect(
      wrapper.get<HTMLSelectElement>('select[aria-label="插件包"]').element.value,
    ).toBe(legacyPackage.id);
    expect(wrapper.get("fieldset").text()).toContain("message.reply.legacy");
    expect(wrapper.get("fieldset").text()).not.toContain("message.forward.current");
    expect(wrapper.findAll("textarea")[0].element.value).toContain(marker);
    expect(wrapper.findAll("textarea")[0].element.value).toContain("/1");

    await wrapper.get('[role="dialog"]').trigger("submit");
    await flushPromises();

    expect(apiMocks.createRevision).toHaveBeenCalledWith(
      deployment.id,
      expect.objectContaining({
        source_revision_id: "revision-1",
        package_version_id: legacyPackage.id,
        config: expect.objectContaining({ api_key: marker }),
        scope: revisions[1].scope,
        grants: revisions[1].grants,
      }),
    );
    expect(apiMocks.activate).toHaveBeenCalledWith(deployment.id, "revision-3");
  });

  it("uses the selected package capabilities and drops incompatible grants", async () => {
    const wrapper = await mountLoadedView();

    await wrapper.get('button[aria-label="升级配置"]').trigger("click");
    await flushPromises();
    const packageSelect = wrapper.get<HTMLSelectElement>('select[aria-label="插件包"]');

    await packageSelect.setValue(legacyPackage.id);

    expect(wrapper.get("fieldset").text()).toContain("message.reply.legacy");
    expect(wrapper.get("fieldset").text()).not.toContain("message.forward.current");
    expect(wrapper.get<HTMLInputElement>("fieldset input").element.checked).toBe(false);
  });

  it("opens the revision switch dialog while the deployment is running", async () => {
    const wrapper = await mountLoadedView();

    await wrapper.get('button[aria-label="启动或回滚"]').trigger("click");

    expect(wrapper.get('[role="dialog"]').text()).toContain("启动或回滚");
    expect(wrapper.get<HTMLSelectElement>('[role="dialog"] select').element.value).toBe(
      "revision-2",
    );
    expect(wrapper.get('button[type="submit"]').text()).toContain("热切换");
  });
});
