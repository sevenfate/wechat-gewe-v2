<script setup lang="ts">
import { Smartphone } from "lucide-vue-next";

import { managementApi } from "@/api/resources";
import type { BotAccount } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import IdentityCell from "@/components/IdentityCell.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime } from "@/utils/format";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<BotAccount>(
  managementApi.accounts.list,
);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="微信账号" description="查看登录、在线与目录同步状态" />

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索昵称、wxid 或 appId"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的微信账号' : '暂无微信账号'">
        <template #icon><Smartphone :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>在线状态</th>
              <th>appId</th>
              <th>同步状态</th>
              <th>最后在线</th>
              <th>备注</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="account in items" :key="account.id">
              <td>
                <IdentityCell :name="account.nickname" :secondary="account.wxid" :avatar-url="account.avatar_url" />
              </td>
              <td><StatusBadge :status="account.status" /></td>
              <td><code>{{ account.app_id }}</code></td>
              <td><StatusBadge :status="account.sync_status" /></td>
              <td>{{ formatDateTime(account.last_online_at) }}</td>
              <td>{{ account.remark || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
