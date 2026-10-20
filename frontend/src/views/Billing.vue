<script setup lang="ts">
import { ref } from 'vue'
import { Coins, RefreshCw, Info, Upload, Plus } from 'lucide-vue-next'
import { api, dateTime, type Page } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'

interface Summary { estimated_usd: string; authoritative_usd: string; invoice_usd: string; adjustments_usd: string; matched_requests: number; matched_difference_usd: string; unbilled_estimates: number; unmatched_invoice_requests: number; entries: number }
interface Entry { entry_id: string; kind: string; provider: string; invoice_id: string; request_id: string | null; amount_usd: string; recorded_at: string; actor: string; reference_id: string | null }
const page = ref(1), busy = ref(false), notice = ref(''), failed = ref(false)
const provider = ref(''), invoiceId = ref(''), lines = ref('[\n  { "line_id": "1", "request_id": "request-id", "amount_usd": "0.001" }\n]')
const adjustmentId = ref(''), reference = ref(''), amount = ref('')
const summary = useResource(signal => api<Summary>('/billing', { signal }))
const entries = useResource(signal => api<Page<Entry>>(`/billing/entries?page=${page.value}`, { signal }), [page])
const usd = (value: string) => '$' + value
const kindName = (value: string) => ({ invoice: '供应商账单', estimate: 'SDK 估算', adjustment: '调整分录' }[value] || value)
async function reload() { await Promise.all([summary.reload(), entries.reload()]) }
async function write(path: string, body: unknown, success: string) {
  busy.value = true; notice.value = ''; failed.value = false
  try { await api(path, { method: 'POST', body: JSON.stringify(body) }); notice.value = success; page.value = 1; await reload() }
  catch (reason) { failed.value = true; notice.value = reason instanceof Error ? reason.message : '操作失败，请重试' }
  finally { busy.value = false }
}
async function importInvoice() {
  let parsed: unknown
  try { parsed = JSON.parse(lines.value) }
  catch { failed.value = true; notice.value = '账单明细必须是合法 JSON 数组。'; return }
  if (!Array.isArray(parsed) || !parsed.length || parsed.length > 1000) { failed.value = true; notice.value = '每次导入需要 1 到 1000 条账单明细。'; return }
  await write('/billing/invoices', { provider: provider.value, invoice_id: invoiceId.value, lines: parsed }, '账单已导入。重复明细已忽略，既有金额不可覆盖。')
}
function adjust() { return write('/billing/adjustments', { adjustment_id: adjustmentId.value, reference_id: reference.value, amount_usd: amount.value }, '调整分录已记录，原始账单保留。') }
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">COST RECONCILIATION</div><h1>成本账本</h1><p>按请求关联 SDK 估算与供应商账单，保留原始分录和调整记录。</p></div><button class="button" :disabled="busy" @click="reload"><RefreshCw :size="15"/>刷新账本</button></div>
  <div class="info-banner"><Info :size="17"/><span>以管理员导入的供应商账单为核算依据；SDK 价格表计算的是近似值。USD 金额以十进制字符串导入，最多 9 位小数。未关联请求的账单需要人工核对。</span></div>
  <ResourceState :loading="summary.loading.value" :error="summary.error.value" @retry="summary.reload"/>
  <template v-if="summary.data.value">
    <div class="metric-grid"><article class="metric-card"><div class="metric-label">SDK 估算<Coins :size="16"/></div><div class="ledger-value">{{ usd(summary.data.value.estimated_usd) }}</div><p class="metric-foot">未开票请求 {{ summary.data.value.unbilled_estimates }}</p></article><article class="metric-card"><div class="metric-label">账单与调整合计</div><div class="ledger-value green">{{ usd(summary.data.value.authoritative_usd) }}</div><p class="metric-foot">原始 {{ usd(summary.data.value.invoice_usd) }} · 调整 {{ usd(summary.data.value.adjustments_usd) }}</p></article><article class="metric-card"><div class="metric-label">已关联请求</div><div class="metric-value">{{ summary.data.value.matched_requests }}</div><p class="metric-foot">待核对账单请求 {{ summary.data.value.unmatched_invoice_requests }}</p></article><article class="metric-card"><div class="metric-label">关联请求金额差异</div><div class="ledger-value">{{ usd(summary.data.value.matched_difference_usd) }}</div><p class="metric-foot">账单及调整减 SDK 估算</p></article></div>
    <div class="heading-actions ledger-actions"><button class="button" :disabled="busy" @click="write('/billing/sync-estimates', {}, '已同步最近 1000 条已结束运行的调用估算。')"><RefreshCw :size="15"/>同步运行估算</button><span class="muted">重复同步不会重复记账</span></div>
    <div v-if="notice" class="form-notice" :class="failed ? 'warning' : 'success'" role="status">{{ notice }}</div>
    <div class="ledger-forms">
      <section class="card policy-mappings"><div class="card-heading"><div><h2><Upload :size="17"/>导入供应商账单</h2><p>供应商、账单号、明细号唯一；金额修正请追加调整分录。</p></div></div><form class="ledger-form" @submit.prevent="importInvoice"><label>供应商<input v-model="provider" required maxlength="128" pattern="[A-Za-z0-9_.:\/\-]+" :disabled="busy" placeholder="例如 provider"/></label><label>账单号<input v-model="invoiceId" required maxlength="128" pattern="[A-Za-z0-9_.:\/\-]+" :disabled="busy" placeholder="例如 invoice-2026-10"/></label><label>账单明细 JSON<textarea v-model="lines" required rows="6" maxlength="200000" spellcheck="false" :disabled="busy"/></label><button class="button primary" :disabled="busy"><Upload :size="15"/>导入账单</button></form></section>
      <section class="card"><div class="card-heading"><div><h2><Plus :size="17"/>追加调整分录</h2><p>从下方选择原始账单；负数表示减免，调整号需唯一。</p></div></div><form class="ledger-form" @submit.prevent="adjust"><label>调整号<input v-model="adjustmentId" required maxlength="128" pattern="[A-Za-z0-9_.:\/\-]+" :disabled="busy"/></label><label>原始分录 ID<input v-model="reference" required maxlength="512" :disabled="busy"/></label><label>调整金额（USD）<input v-model="amount" required maxlength="40" pattern="-?[0-9]+(\.[0-9]{1,9})?" placeholder="例如 -0.001" :disabled="busy" inputmode="decimal"/></label><button class="button" :disabled="busy"><Plus :size="15"/>记录调整</button></form></section>
    </div>
  </template>
  <section class="card"><div class="card-heading"><div><h2>不可变分录</h2><p>账单与估算分别保留，不覆盖历史金额。</p></div></div><ResourceState :loading="entries.loading.value" :error="entries.error.value" @retry="entries.reload"/><div v-if="entries.data.value" class="table-scroll"><table class="runs-table ledger-table"><thead><tr><th>时间</th><th>类型 / 供应商</th><th>请求 / 账单</th><th>金额（USD）</th><th>分录</th></tr></thead><tbody><tr v-for="entry in entries.data.value.items" :key="entry.entry_id"><td>{{ dateTime(entry.recorded_at) }}</td><td>{{ kindName(entry.kind) }}<small>{{ entry.provider }}</small></td><td>{{ entry.request_id || '未关联请求' }}<small>{{ entry.invoice_id || 'SDK 估算' }}</small></td><td class="mono">{{ entry.amount_usd }}</td><td><button v-if="entry.kind === 'invoice'" class="text-link" @click="reference = entry.entry_id">用于调整</button><small class="mono">{{ entry.entry_id }}</small></td></tr><tr v-if="!entries.data.value.total"><td colspan="5">暂无分录。同步运行估算或导入账单后可查看。</td></tr></tbody></table></div><div v-if="entries.data.value" class="pagination"><span>共 {{ entries.data.value.total }} 条</span><button class="button" :disabled="page === 1" @click="page--">上一页</button><span>{{ page }}</span><button class="button" :disabled="page * 50 >= entries.data.value.total" @click="page++">下一页</button></div></section>
</template>

<style>
.ledger-value{font-size:24px;margin-top:15px;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.ledger-actions{margin:0 0 20px;flex-wrap:wrap}.ledger-forms{display:grid;grid-template-columns:1.2fr 1fr;gap:20px;margin:0 0 24px}.ledger-form{padding:0 22px 22px;display:grid;gap:17px}.ledger-form label{display:grid;gap:8px;color:#65776c;font-size:11px}.ledger-form input{border:1px solid #dfe6e2;border-radius:6px;padding:10px;width:100%}.ledger-form button{justify-self:start}.table-scroll{overflow-x:auto;max-width:100%}.ledger-table{min-width:740px}.ledger-table small{display:block;color:#8b968f;font-size:10px;margin-top:5px;max-width:320px;overflow-wrap:anywhere}.ledger-form textarea{font:11px/1.8 Consolas,monospace;border:1px solid #dfe6e2;border-radius:6px;padding:10px;width:100%;resize:vertical;background:#fafcfb}.ledger-table td{vertical-align:top}@media(max-width:1000px){.ledger-forms{grid-template-columns:1fr}}
</style>
