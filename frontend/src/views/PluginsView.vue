<script setup lang="ts">
import { PackageOpen } from "lucide-vue-next";

import { managementApi } from "@/api/resources";
import type { PluginSummary } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<PluginSummary>(
  managementApi.plugins.list,
);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="插件" description="查看私有插件、信任状态与运行健康" />

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索插件名称或来源"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的插件' : '插件库为空'">
        <template #icon><PackageOpen :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>插件</th>
              <th>版本</th>
              <th>信任状态</th>
              <th>运行状态</th>
              <th>部署</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="plugin in items" :key="plugin.id">
              <td>
                <div class="plugin-cell">
                  <span class="resource-icon"><PackageOpen :size="17" /></span>
                  <span>
                    <strong>{{ plugin.name }}</strong>
                    <small v-if="plugin.description">{{ plugin.description }}</small>
                  </span>
                </div>
              </td>
              <td><code>{{ plugin.latest_version || "-" }}</code></td>
              <td><StatusBadge :status="plugin.trust_status" /></td>
              <td><StatusBadge :status="plugin.health_status || plugin.status" /></td>
              <td>{{ plugin.deployment_count ?? "-" }}</td>
              <td>{{ plugin.source || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
