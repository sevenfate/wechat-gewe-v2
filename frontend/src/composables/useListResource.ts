import { onBeforeUnmount, onMounted, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";
import type { ListResult } from "@/api/types";

export function useListResource<T>(loader: (search: string) => Promise<ListResult<T>>) {
  const items = shallowRef<T[]>([]);
  const total = ref<number | null>(null);
  const loading = ref(false);
  const error = shallowRef<ApiError | null>(null);
  const search = ref("");
  let loadEpoch = 0;

  async function reload() {
    const requestEpoch = ++loadEpoch;
    loading.value = true;
    error.value = null;
    try {
      const result = await loader(search.value.trim());
      if (requestEpoch !== loadEpoch) return;
      items.value = result.items;
      total.value = result.total;
    } catch (caught) {
      if (requestEpoch !== loadEpoch) return;
      error.value =
        caught instanceof ApiError
          ? caught
          : new ApiError("加载数据时发生未知错误", 0, "UNKNOWN_ERROR");
      items.value = [];
      total.value = null;
    } finally {
      if (requestEpoch === loadEpoch) loading.value = false;
    }
  }

  function clearSearch() {
    if (!search.value) return;
    search.value = "";
    void reload();
  }

  onMounted(reload);
  onBeforeUnmount(() => {
    loadEpoch += 1;
  });

  return { items, total, loading, error, search, reload, clearSearch };
}
