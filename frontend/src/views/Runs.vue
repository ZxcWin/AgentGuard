<script setup lang="ts">
import { computed, ref, watch, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, RefreshCw, ChevronLeft, ChevronRight, X, ArrowUpRight, GitBranch, Copy, Check, ShieldCheck } from 'lucide-vue-next'
import { api, number, money, dateTime, sourceName, statusName, type Page, type Run, type RunEvent } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'
import CallTree from '../components/CallTree.vue'
import { useModal } from '../useModal'
const route = useRoute(), router = useRouter()
const hours = ref(24), status = ref(''), search = ref(''), query = ref(''), page = ref(1)
let debounce: ReturnType<typeof setTimeout>
watch(search, value => { clearTimeout(debounce); debounce = setTimeout(() => { query.value = value; page.value = 1 }, 300) })
watch([hours, status], () => { page.value = 1 })
onUnmounted(() => clearTimeout(debounce))
const { data, loading, error, reload } = useResource(signal => api<Page<Run>>(`/runs?hours=${hours.value}&status=${status.value}&search=${encodeURIComponent(query.value)}&page=${page.value}&page_size=10`, { signal }), [hours, status, query, page])
const selected = ref<Run | null>(null), detailLoading = ref(false), detailError = ref(''), copied = ref(false)
const drawer = ref<HTMLElement | null>(null)
const detailOpen = computed(() => typeof route.query.run === 'string' && !!route.query.run)
const trapFocus = useModal(drawer, detailOpen, closeRun)
const appRoot = document.getElementById('app')
const previousInert = appRoot?.inert ?? false
watch(detailOpen, open => { if (appRoot) appRoot.inert = open || previousInert }, { immediate: true })
onUnmounted(() => { if (appRoot) appRoot.inert = previousInert })
const loadingEvents = ref(false)
let detailController: AbortController | null = null
async function openRun(id: string) { await router.replace({ query: { ...route.query, run: id } }) }
function closeRun() { router.replace({ query: {} }) }
async function loadDetail() {
  detailController?.abort(); detailController = new AbortController()
  const runId = typeof route.query.run === 'string' ? route.query.run : ''
  selected.value = null; detailError.value = ''
  if (!runId) return
  detailLoading.value = true
  try {
    const result = await api<Run>('/runs/' + encodeURIComponent(runId), { signal: detailController.signal })
    if (route.query.run === runId) selected.value = result
  } catch (reason) { if (route.query.run === runId) detailError.value = reason instanceof Error ? reason.message : '加载失败' }
  finally { if (route.query.run === runId) { detailLoading.value = false; await nextTick(); drawer.value?.focus() } }
}
watch(() => route.query.run, loadDetail, { immediate: true })
onUnmounted(() => detailController?.abort())
async function copy(value: string) { try { await navigator.clipboard.writeText(value); copied.value = true; setTimeout(() => { copied.value = false }, 2000) } catch { detailError.value = '浏览器无法复制，请手动选择 Run ID' } }
async function loadMoreEvents() {
  if (!selected.value) return
  const runId = selected.value.run_id
  const generation = detailController
  loadingEvents.value = true
  try {
    const result = await api<Page<RunEvent>>(`/runs/${encodeURIComponent(runId)}/events?page=${Math.floor(selected.value.events.length / 100) + 1}&page_size=100`, { signal: detailController?.signal })
    if (selected.value?.run_id === runId && generation === detailController) {
      const ids = new Set(selected.value.events.map(event => event.id))
      selected.value.events.push(...result.items.filter(event => !ids.has(event.id)))
      selected.value.events_total = result.total
    }
  } catch (reason) { if (generation === detailController && !generation?.signal.aborted) detailError.value = reason instanceof Error ? reason.message : '加载事件失败' }
  finally { if (generation === detailController) loadingEvents.value = false }
}
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">RUN EXPLORER</div><h1>运行记录</h1><p>从请求到结算，查看 Agent 运行的完整事件轨迹。</p></div><button class="button" :disabled="loading" @click="reload"><RefreshCw :size="15" :class="{ spin: loading }"/>刷新记录</button></div>
  <section class="card"><div class="filter-bar"><label class="search-field"><Search :size="17"/><input v-model="search" placeholder="搜索 Agent 名称或 Run ID…" aria-label="搜索运行" maxlength="100"/></label><div class="filter-selects"><select v-model="status" aria-label="运行状态"><option value="">全部状态</option><option value="completed">已完成</option><option value="failed">失败</option><option value="running">运行中</option><option value="cancelled">已取消</option><option value="timed_out">已超时</option></select><select v-model.number="hours" aria-label="时间范围"><option :value="1">最近 1 小时</option><option :value="24">最近 24 小时</option><option :value="168">最近 7 天</option></select></div></div>
  <ResourceState :loading="loading" :error="error" :empty="!loading && !error && !data?.items.length" message="当前条件下没有运行记录，试试调整筛选条件。" @retry="reload"/>
  <template v-if="!loading && !error && data?.items.length"><div class="table-scroll"><table><thead><tr><th>Agent / Run ID</th><th>状态</th><th>调用</th><th>Token 消耗</th><th>估算成本</th><th>Usage 来源</th><th>开始时间</th><th></th></tr></thead><tbody><tr v-for="run in data.items" :key="run.run_id"><td><button class="run-name" @click="openRun(run.run_id)">{{ run.agent_name }}</button><small class="mono muted run-id">{{ run.run_id.slice(0, 18) }}…</small></td><td><span class="status-badge" :class="run.status"><span class="tiny-dot"></span>{{ statusName(run.status) }}</span></td><td>{{ run.llm_calls }}</td><td class="tabular">{{ number(run.total_tokens) }}</td><td class="tabular">{{ run.cost_known ? money(run.estimated_cost_usd) : '价格缺失' }}</td><td><span class="subtle-tag">{{ sourceName(run.usage_source) }}</span></td><td class="muted nowrap">{{ dateTime(run.started_at) }}</td><td><button class="icon-button" :aria-label="'查看 ' + run.agent_name + ' 详情'" @click="openRun(run.run_id)"><ArrowUpRight :size="16"/></button></td></tr></tbody></table></div><div class="pagination"><span>共 {{ number(data.total) }} 条记录 · 第 {{ page }} / {{ Math.max(1, Math.ceil(data.total / 10)) }} 页</span><div><button class="button square" :disabled="page <= 1" aria-label="上一页" @click="page--"><ChevronLeft :size="16"/></button><button class="button square" :disabled="page * 10 >= data.total" aria-label="下一页" @click="page++"><ChevronRight :size="16"/></button></div></div></template></section>
  <div class="page-note"><ShieldCheck :size="14"/>运行记录仅展示元数据。成本为 SDK 估算值，非供应商账单。</div>
  <Teleport to="body"><div v-if="route.query.run" class="drawer-overlay" @click.self="closeRun"><section ref="drawer" tabindex="-1" class="run-drawer" role="dialog" aria-modal="true" aria-labelledby="run-detail-title" @keydown="trapFocus"><div class="drawer-heading"><div><span class="eyebrow">RUN DETAILS</span><h2 id="run-detail-title">运行详情</h2></div><button class="icon-button" title="关闭详情" @click="closeRun"><X :size="20"/></button></div><ResourceState :loading="detailLoading" :error="detailError" @retry="loadDetail"/><template v-if="selected"><h3 class="detail-agent-name">{{ selected.agent_name }}</h3><div class="detail-run-id"><code>{{ selected.run_id }}</code><button class="icon-button" title="复制 Run ID" @click="copy(selected.run_id)"><Check v-if="copied" :size="14"/><Copy v-else :size="14"/></button></div><div class="detail-status-row"><span class="status-badge" :class="selected.status"><span class="tiny-dot"></span>{{ statusName(selected.status) }}</span><span class="subtle-tag">策略 v{{ selected.config_version }}</span></div><div class="detail-metrics"><div><small>模型调用</small><strong>{{ selected.llm_calls }} 次</strong></div><div><small>Token 消耗</small><strong>{{ number(selected.total_tokens) }}</strong></div><div><small>估算成本</small><strong>{{ selected.cost_known ? money(selected.estimated_cost_usd) : '价格缺失' }}</strong></div><div><small>Usage 来源</small><strong>{{ sourceName(selected.usage_source) }}</strong></div></div><dl class="detail-meta"><div><dt>开始时间</dt><dd>{{ dateTime(selected.started_at) }}</dd></div><div><dt>Trace ID</dt><dd class="mono">{{ selected.trace_id }}</dd></div><div><dt>工具调用</dt><dd>{{ selected.tool_calls }} 次</dd></div></dl><p v-if="selected.retry_count" class="page-note">已执行 {{ selected.retry_count }} 次重试；每次尝试均独立准入并计入预算。</p><CallTree :events="selected.events" :total="selected.events_total"/><h3 class="timeline-title"><GitBranch :size="17"/>事件时间线<span>{{ selected.events_total }} 个事件</span></h3><ol class="timeline"><li v-for="event in selected.events" :key="event.id"><span class="timeline-dot" :class="{ failed: event.event.includes('failed') || event.event.includes('rejected') }"></span><div><strong class="mono">{{ event.event }}</strong><small>{{ dateTime(event.timestamp) }}</small><code v-if="event.request_id">{{ event.request_id }}</code><span v-if="event.reason_code" class="danger">{{ event.reason_code }}</span></div></li></ol><button v-if="selected.events.length < selected.events_total" class="button events-more" :disabled="loadingEvents" @click="loadMoreEvents">{{ loadingEvents ? '正在加载…' : '加载更多事件' }}</button><div class="privacy-note"><ShieldCheck :size="17"/><p>事件已按允许字段存储，不包含模型输入与输出原文。</p></div></template></section></div></Teleport>
</template>
