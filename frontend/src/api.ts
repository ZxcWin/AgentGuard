export interface Health { status: string; api_version: string; sdk_version: string; schema_version: number; demo: boolean; storage: string; policy_scope: string; policy_persistence: string }
export interface Run { run_id: string; trace_id: string; agent_name: string; status: string; started_at: string; ended_at: string | null; duration_ms: number | null; total_tokens: number; estimated_cost_usd: string; llm_calls: number; tool_calls: number; retry_count: number; usage_source: string; config_version: number; blocked: number; events: RunEvent[]; events_total: number; cost_known: boolean }
export interface RunEvent { id: string; event: string; timestamp: string; request_id?: string; reason_code?: string; [key: string]: unknown }
export interface Page<T> { items: T[]; total: number; page: number; page_size: number }
export interface OverviewData { hours: number; timezone: string; runs: number; calls: number; tokens: number; failed: number; blocked: number; estimated_cost_usd: string; sources: Record<string, number>; missing_price_runs: number; models: string[]; series: { timestamp: string; calls: number; tokens: number; failed: number }[]; agents: { name: string; runs: number; tokens: number; cost: string; failed: number }[] }
export type PolicyValue = number | string | boolean | null | PolicyValue[] | { [key: string]: PolicyValue }
export type PolicyConfig = Record<string, Record<string, PolicyValue>>
export interface Policy { version: number; config: PolicyConfig; editable: Record<string, string[]>; scope: string; persistence: string }
export class ApiError extends Error { constructor(public status: number, message: string) { super(message) } }
export const apiBase = import.meta.env.VITE_API_BASE || '/api/v1'
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const timeout = AbortSignal.timeout(10000)
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout
  const response = await fetch(apiBase + path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers }, signal })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status, response.status === 401 || response.status === 403 ? '无权访问该资源，请确认当前连接与访问来源。' : data.error?.message || `请求失败 (${response.status})`)
  return data as T
}
export const number = (value: number) => new Intl.NumberFormat('en-US').format(value)
export const money = (value: string | number) => '$' + Number(value).toFixed(4)
export const dateTime = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
export const sourceName = (value: string) => ({ provider: '供应商', estimate: '估算', tokenizer: '分词器', unknown: '未知', mixed: '混合来源' }[value] || value)
export const statusName = (value: string) => ({ completed: '已完成', running: '运行中', failed: '失败', cancelled: '已取消', timed_out: '已超时' }[value] || value)
export const apiDocsUrl = apiBase.replace(/\/api\/v1\/?$/, '/docs')
