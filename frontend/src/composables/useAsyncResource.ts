import { onBeforeUnmount, onMounted, ref, shallowRef } from "vue";

import { ApiError } from "@/api/client";

export function useAsyncResource<T>(loader: () => Promise<T>) {
  const data = shallowRef<T | null>(null);
  const loading = ref(false);
  const error = shallowRef<ApiError | null>(null);
  let loadEpoch = 0;

  async function reload() {
    const requestEpoch = ++loadEpoch;
    loading.value = true;
    error.value = null;
    try {
      const result = await loader();
      if (requestEpoch !== loadEpoch) return;
      data.value = result;
    } catch (caught) {
      if (requestEpoch !== loadEpoch) return;
      error.value =
        caught instanceof ApiError
          ? caught
          : new ApiError("加载数据时发生未知错误", 0, "UNKNOWN_ERROR");
    } finally {
      if (requestEpoch === loadEpoch) loading.value = false;
    }
  }

  onMounted(reload);
  onBeforeUnmount(() => {
    loadEpoch += 1;
  });

  return { data, loading, error, reload };
}
