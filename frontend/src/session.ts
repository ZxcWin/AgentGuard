import { ref } from 'vue'

// Bearer credentials stay in memory, never in URLs, storage, or build variables.
export const workspaceToken = ref('')
export const authRequired = ref(false)
export const principal = ref<{ subject: string; role: string } | null>(null)
export function clearSession() { workspaceToken.value = ''; principal.value = null }
