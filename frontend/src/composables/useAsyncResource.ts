import { onMounted, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";

export function useAsyncResource<T>(loader: () => Promise<T>) {
  const data = shallowRef<T | null>(null);
  const loading = ref(false);
  const error = shallowRef<ApiError | null>(null);

  async function reload() {
    loading.value = true;
    error.value = null;
    try {
      data.value = await loader();
    } catch (caught) {
      error.value =
        caught instanceof ApiError
          ? caught
          : new ApiError("加载数据时发生未知错误", 0, "UNKNOWN_ERROR");
    } finally {
      loading.value = false;
    }
  }

  onMounted(reload);

  return { data, loading, error, reload };
}
