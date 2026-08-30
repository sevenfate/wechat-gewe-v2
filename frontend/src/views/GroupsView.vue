<script setup lang="ts">
import { UsersRound } from "lucide-vue-next";

import { managementApi } from "@/api/resources";
import type { DiscoveredGroup } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import IdentityCell from "@/components/IdentityCell.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime, formatInteger } from "@/utils/format";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<DiscoveredGroup>(
  managementApi.groups.list,
);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="已发现群" description="通讯录同步或消息中已被系统发现的群聊" />

    <section class="notice-band">
      <UsersRound :size="18" />
      <span>此列表不代表微信账号加入过的全部历史群。</span>
    </section>

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索群名或 chatroom ID"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的群' : '暂无已发现群'">
        <template #icon><UsersRound :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>群聊</th>
              <th>群主</th>
              <th>成员</th>
              <th>发现来源</th>
              <th>新鲜度</th>
              <th>更新时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="group in items" :key="group.id">
              <td>
                <IdentityCell
                  :name="group.name"
                  :secondary="group.chatroom_id"
                  :avatar-url="group.avatar_url"
                  square
                />
              </td>
              <td><code>{{ group.owner_wxid || "-" }}</code></td>
              <td>{{ formatInteger(group.member_count) }}</td>
              <td>{{ group.discovery_source || "-" }}</td>
              <td><StatusBadge :status="group.freshness" /></td>
              <td>{{ formatDateTime(group.updated_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
