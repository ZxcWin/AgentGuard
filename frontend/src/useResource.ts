import { onUnmounted, ref, watch, type WatchSource } from 'vue'
import { refreshKey } from './state'

export function useResource<T>(fetcher: (signal: AbortSignal) => Promise<T>, dependencies: WatchSource[] = []) {
  const data = ref<T | null>(null)
  const loading = ref(true)
  const error = ref('')
  let controller: AbortController | null = null
  let generation = 0
  async function reload() {
    controller?.abort()
    controller = new AbortController()
    const current = ++generation
    loading.value = true
    error.value = ''
    try {
      const result = await fetcher(controller.signal)
      if (current === generation) data.value = result
    } catch (reason) {
      if (current === generation && !(reason instanceof DOMException && reason.name === 'AbortError')) {
        data.value = null
        error.value = reason instanceof Error ? reason.message : '请求失败，请重试'
      }
    } finally { if (current === generation) loading.value = false }
  }
  watch([refreshKey, ...dependencies], reload, { immediate: true })
  onUnmounted(() => { generation++; controller?.abort() })
  return { data, loading, error, reload }
}
