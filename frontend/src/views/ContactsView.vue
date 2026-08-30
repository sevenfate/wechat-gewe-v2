<script setup lang="ts">
import { UserRound } from "lucide-vue-next";

import { managementApi } from "@/api/resources";
import type { Contact } from "@/api/types";
import EmptyState from "@/components/EmptyState.vue";
import ErrorState from "@/components/ErrorState.vue";
import IdentityCell from "@/components/IdentityCell.vue";
import LoadingState from "@/components/LoadingState.vue";
import PageHeader from "@/components/PageHeader.vue";
import ResourceToolbar from "@/components/ResourceToolbar.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { useListResource } from "@/composables/useListResource";
import { formatDateTime } from "@/utils/format";

const { items, total, loading, error, search, reload, clearSearch } = useListResource<Contact>(
  managementApi.contacts.list,
);
</script>

<template>
  <div class="page-stack">
    <PageHeader title="联系人" description="持久化通讯录身份与最近同步信息" />

    <section class="data-panel">
      <ResourceToolbar
        v-model="search"
        :loading="loading"
        :total="total"
        placeholder="搜索昵称、备注或 wxid"
        @search="reload"
        @clear="clearSearch"
        @refresh="reload"
      />

      <LoadingState v-if="loading && !items.length" />
      <ErrorState v-else-if="error" :error="error" @retry="reload" />
      <EmptyState v-else-if="!items.length" :title="search ? '没有匹配的联系人' : '暂无联系人数据'">
        <template #icon><UserRound :size="23" /></template>
      </EmptyState>
      <div v-else class="table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>联系人</th>
              <th>备注</th>
              <th>类型</th>
              <th>状态</th>
              <th>最后同步</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="contact in items" :key="contact.id">
              <td><IdentityCell :name="contact.nickname" :secondary="contact.external_id" /></td>
              <td>{{ contact.remark || "-" }}</td>
              <td>{{ contact.contact_type || "-" }}</td>
              <td><StatusBadge :status="contact.status" /></td>
              <td>{{ formatDateTime(contact.last_synced_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
