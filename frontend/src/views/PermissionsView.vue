<script setup lang="ts">
import { Save, ShieldCheck } from "lucide-vue-next";
import { computed, ref } from "vue";

import { managementApi } from "@/api/resources";
import type { PermissionEffect } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import { useAsyncResource } from "@/composables/useAsyncResource";

const { data, loading, error, reload } = useAsyncResource(managementApi.permissions.matrix);
const search = ref("");

const filteredGroups = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  if (!keyword) return data.value?.groups || [];
  return (data.value?.groups || []).filter((group) =>
    [group.name, group.account_name].some((value) => value?.toLocaleLowerCase().includes(keyword)),
  );
});

function effectFor(groupId: string, resourceId: string): PermissionEffect {
  return (
    data.value?.rules.find((rule) => rule.group_id === groupId && rule.resource_id === resourceId)?.effect ||
    "INHERIT"
  );
}

function effectLabel(effect: PermissionEffect): string {
  return { INHERIT: "继承", ALLOW: "允许", DENY: "拒绝" }[effect];
}
</script>

<template>
  <div class="page-stack">
    <PageHeader title="权限矩阵" description="按已发现群查看插件、Connector 与 Task Agent 的有效规则">
      <template #actions>
        <button class="button button--primary" type="button" disabled>
          <Save :size="16" />
          保存变更
        </button>
      </template>
    </PageHeader>

    <section class="notice-band notice-band--neutral">
      <ShieldCheck :size="18" />
      <span>成员例外和 locked deny 将覆盖此处的群级设置。</span>
    </section>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="filteredGroups.length"
        placeholder="搜索群或所属账号"
        @clear="search = ''"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !data" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState
        v-else-if="!data?.groups.length || !data.resources.length"
        title="暂无权限矩阵数据"
        detail="已发现群和可授权资源就绪后将在此显示"
      >
        <template #icon><ShieldCheck :size="23" /></template>
      </EmptyState>
      <EmptyState v-else-if="!filteredGroups.length" title="没有匹配的群" />
      <div v-else class="table-scroll matrix-scroll">
        <table class="data-table permission-matrix">
          <thead>
            <tr>
              <th class="matrix-group-column">已发现群</th>
              <th v-for="resource in data.resources" :key="resource.id">
                <span class="matrix-resource-heading">
                  <strong>{{ resource.name }}</strong>
                  <small>{{ resource.type }}</small>
                </span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="group in filteredGroups" :key="group.id">
              <td class="matrix-group-column">
                <strong>{{ group.name }}</strong>
                <small>{{ group.account_name || "-" }}</small>
              </td>
              <td v-for="resource in data.resources" :key="resource.id">
                <span
                  class="permission-effect"
                  :class="`permission-effect--${effectFor(group.id, resource.id).toLocaleLowerCase()}`"
                >
                  {{ effectLabel(effectFor(group.id, resource.id)) }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
