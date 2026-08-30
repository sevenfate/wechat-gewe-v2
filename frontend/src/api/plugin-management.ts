import { apiRequest } from "./client";

export interface PluginManifest {
  id: string;
  name: string;
  version: string;
  description?: string;
  capabilities?: string[];
  config_schema?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ManagedPlugin {
  id: string;
  workspace_id: string;
  plugin_id: string;
  name: string;
  description: string;
  retired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ManagedPluginPackage {
  id: string;
  plugin_id: string;
  semantic_version: string;
  package_sha256: string;
  manifest: PluginManifest;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface ManagedPluginDeployment {
  id: string;
  workspace_id: string;
  plugin_id: string;
  name: string;
  status: string;
  active_revision_id: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ManagedPluginRevision {
  id: string;
  deployment_id: string;
  package_version_id: string;
  revision_number: number;
  config_fingerprint: string;
  scope: Record<string, unknown>;
  grants: string[];
  content_sha256: string;
  created_at: string;
  updated_at: string;
}

export interface ManagedPluginCatalog {
  plugins: ManagedPlugin[];
  packages: ManagedPluginPackage[];
  deployments: ManagedPluginDeployment[];
  revisions: ManagedPluginRevision[];
}

export interface ManagedPluginContext {
  workspace_id: string;
  name: string;
}

export interface ManagedPluginRevisionDraft {
  source_revision_id: string;
  package_version_id: string;
  config: Record<string, unknown>;
  scope: Record<string, unknown>;
  grants: string[];
  secret_placeholder: string;
}

export interface DeploymentInput {
  package_version_id: string;
  config: Record<string, unknown>;
  scope: Record<string, unknown>;
  grants: string[];
}

export interface RevisionInput {
  source_revision_id?: string;
  package_version_id?: string;
  config?: Record<string, unknown>;
  scope?: Record<string, unknown>;
  grants?: string[];
}

export const pluginManagementApi = {
  context: () => apiRequest<ManagedPluginContext>("/plugins/context"),
  catalog: () => apiRequest<ManagedPluginCatalog>("/plugins"),
  installBuiltin: (pluginId: string, workspaceId: string) =>
    apiRequest<{ plugin: ManagedPlugin; package: ManagedPluginPackage }>(
      `/plugins/builtins/${encodeURIComponent(pluginId)}/install`,
      { method: "POST", body: JSON.stringify({ workspace_id: workspaceId }) },
    ),
  createDeployment: (input: DeploymentInput & {
    workspace_id: string;
    plugin_id: string;
    name: string;
  }) =>
    apiRequest<{ deployment: ManagedPluginDeployment; revision: ManagedPluginRevision }>(
      "/plugins/deployments",
      { method: "POST", body: JSON.stringify(input) },
    ),
  createRevision: (deploymentId: string, input: RevisionInput) =>
    apiRequest<ManagedPluginRevision>(
      `/plugins/deployments/${encodeURIComponent(deploymentId)}/revisions`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  revisionDraft: (deploymentId: string, revisionId: string) =>
    apiRequest<ManagedPluginRevisionDraft>(
      `/plugins/deployments/${encodeURIComponent(deploymentId)}/revisions/${encodeURIComponent(revisionId)}/draft`,
    ),
  activate: (deploymentId: string, revisionId: string) =>
    apiRequest<{ deployment: ManagedPluginDeployment }>(
      `/plugins/deployments/${encodeURIComponent(deploymentId)}/revisions/${encodeURIComponent(revisionId)}/activate`,
      { method: "POST" },
    ),
  deactivate: (deploymentId: string) =>
    apiRequest<ManagedPluginDeployment>(
      `/plugins/deployments/${encodeURIComponent(deploymentId)}/deactivate`,
      { method: "POST" },
    ),
  health: (deploymentId: string) =>
    apiRequest<{ activation_epoch: number; result: unknown }>(
      `/plugins/deployments/${encodeURIComponent(deploymentId)}/invoke`,
      { method: "POST", body: JSON.stringify({ method: "health", params: {} }) },
    ),
};
