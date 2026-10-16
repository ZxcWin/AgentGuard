import { ref } from 'vue'
import { api, type Health } from './api'
export const health = ref<Health | null>(null)
export const connectionError = ref('')
export const refreshKey = ref(0)
export async function checkHealth() {
  try { health.value = await api<Health>('/health'); connectionError.value = '' }
  catch (error) { health.value = null; connectionError.value = error instanceof Error ? error.message : '连接失败' }
}
