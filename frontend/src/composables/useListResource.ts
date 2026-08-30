import { onMounted, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";
import type { ListResult } from "@/api/types";

export function useListResource<T>(loader: (search: string) => Promise<ListResult<T>>) {
  const items = shallowRef<T[]>([]);
  const total = ref<number | null>(null);
  const loading = ref(false);
  const error = shallowRef<ApiError | null>(null);
  const search = ref("");

  async function reload() {
    loading.value = true;
    error.value = null;
    try {
      const result = await loader(search.value.trim());
      items.value = result.items;
      total.value = result.total;
    } catch (caught) {
      error.value =
        caught instanceof ApiError
          ? caught
          : new ApiError("加载数据时发生未知错误", 0, "UNKNOWN_ERROR");
      items.value = [];
      total.value = null;
    } finally {
      loading.value = false;
    }
  }

  function clearSearch() {
    if (!search.value) return;
    search.value = "";
    void reload();
  }

  onMounted(reload);

  return { items, total, loading, error, search, reload, clearSearch };
}
