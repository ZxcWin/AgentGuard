<script setup lang="ts">
import { ref } from 'vue'
import { RefreshCw, ShieldCheck, Info } from 'lucide-vue-next'
import { api, dateTime, type Page } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'
const page = ref(1), pending = ref(false), notice = ref(''), recovering = ref(false)
const resolveOperation = ref(''), resolveOutcome = ref('uncertain_recorded')
interface AuditEvent { event_id: string; operation_id: string; recorded_at: string; kind: string; action?: string; outcome?: string; actor_id?: string; delivered_at: string | null; run_id?: string; model?: string; tool?: string; execution_may_have_completed?: boolean }
const actionName = (value?: string) => ({ 'tool.invoke': '工具执行', 'llm.invoke': '模型调用', 'billing.sync_estimates': '同步估算', 'billing.import': '导入账单', 'billing.adjust': '账单调整', 'policy.publish': '发布策略', 'policy.update': '更新策略', 'tool.approval': '工具审批', 'security.check': '安全检查', 'permission.check': '权限检查', 'budget.reject': '预算拒绝' }[value || ''] || value || '')
interface AuditHealth { records: number; pending: number; bytes: number; max_bytes: number; unresolved_operations: number; retention_days: number; blocked: boolean }
const records = useResource(signal => api<Page<AuditEvent>>(`/audit?page=${page.value}&pending_only=${pending.value}`, { signal }), [page, pending])
const status = useResource(signal => api<AuditHealth>('/audit/health', { signal }))
async function reload() { await Promise.all([records.reload(), status.reload()]) }
async function recover() {
  recovering.value = true; notice.value = ''
  try { await api('/audit/recover', { method: 'POST' }); notice.value = '已写入恢复标记并解除暂停；不确定操作不会自动重放，请核对对应操作 ID。'; await reload() }
  catch (reason) { notice.value = reason instanceof Error ? reason.message : '恢复失败' }
  finally { recovering.value = false }
}
async function reconcile() {
  recovering.value = true; notice.value = ''
  try { await api('/audit/reconcile', { method: 'POST', body: JSON.stringify({ operation_id: resolveOperation.value, outcome: resolveOutcome.value }) }); notice.value = '核对结果已记录，未重放原操作。'; resolveOperation.value = ''; await reload() }
  catch (reason) { notice.value = reason instanceof Error ? reason.message : '核对失败' }
  finally { recovering.value = false }
}
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">DURABLE AUDIT</div><h1>审计记录</h1><p>操作执行前持久记录意图，执行后记录结果；通过操作 ID 关联。</p></div><button class="button" @click="reload"><RefreshCw :size="15"/>刷新审计</button></div>
  <ResourceState :loading="status.loading.value" :error="status.error.value" @retry="status.reload"/>
  <section v-if="status.data.value" class="card settings-connection"><div class="card-heading"><div><h2><ShieldCheck :size="18"/>审计存储</h2><p>本地 SQLite FULL 同步写入；投递采用至少一次语义。</p></div><span class="status-badge" :class="status.data.value.blocked ? 'failed' : 'completed'">{{ status.data.value.blocked ? '保护操作已暂停' : '写入正常' }}</span></div><dl class="connection-grid"><div><dt>总记录 / 待投递</dt><dd>{{ status.data.value.records }} / {{ status.data.value.pending }}</dd></div><div><dt>未见结果的操作</dt><dd>{{ status.data.value.unresolved_operations }}</dd></div><div><dt>有效数据容量</dt><dd>{{ (status.data.value.bytes / 1048576).toFixed(2) }} / {{ (status.data.value.max_bytes / 1048576).toFixed(0) }} MiB</dd></div><div><dt>默认审计保留</dt><dd>{{ status.data.value.retention_days }} 天</dd></div></dl><button v-if="status.data.value.blocked" class="button" :disabled="recovering" @click="recover">存储修复后解除暂停</button><p v-if="notice" role="status">{{ notice }}</p></section>
  <div class="info-banner"><Info :size="17"/><span>无结果可能代表调用尚在执行或进程中断，不能据此重放副作用。未投递与未核对记录不自动清理，普通遥测保留期不影响审计。</span></div>
  <section v-if="status.data.value?.unresolved_operations" class="card settings-connection"><div class="card-heading"><div><h2>不确定操作核对</h2><p>先核对真实系统的执行结果，再记录结论；记录不确定状态也不会自动重放。</p></div></div><form @submit.prevent="reconcile"><div class="policy-selects"><label>操作 ID<input v-model="resolveOperation" required maxlength="256"/></label><label>核对结论<select v-model="resolveOutcome"><option value="uncertain_recorded">仍不确定，记录并交人工跟踪</option><option value="confirmed_completed">已核实完成</option><option value="confirmed_not_executed">已核实未执行</option></select></label></div><button class="button" :disabled="recovering">记录核对结论</button></form></section>
  <section class="card"><div class="card-heading"><div><h2>操作与投递记录</h2></div><label><input v-model="pending" type="checkbox" @change="page = 1"/>仅看待投递</label></div><ResourceState :loading="records.loading.value" :error="records.error.value" @retry="records.reload"/><div v-if="records.data.value" class="table-scroll"><table class="runs-table"><thead><tr><th>记录时间</th><th>操作</th><th>阶段 / 结果</th><th>操作 ID</th><th>投递</th></tr></thead><tbody><tr v-for="event in records.data.value.items" :key="event.event_id"><td>{{ dateTime(event.recorded_at) }}</td><td>{{ actionName(event.action) || event.tool || event.model || event.actor_id || '—' }}</td><td>{{ event.kind }} / {{ event.outcome || '—' }}<small v-if="event.execution_may_have_completed &amp;&amp; !['completed','published','confirmed_completed'].includes(event.outcome || '')">操作可能已执行</small></td><td class="mono">{{ event.operation_id }}</td><td>{{ event.delivered_at ? '已投递' : '待投递' }}</td></tr><tr v-if="!records.data.value.total"><td colspan="5">暂无审计记录。发布策略或执行示例后可查看。</td></tr></tbody></table></div><div v-if="records.data.value" class="pagination"><span>共 {{ records.data.value.total }} 条</span><button class="button" :disabled="page === 1" @click="page--">上一页</button><span>{{ page }}</span><button class="button" :disabled="page * 50 >= records.data.value.total" @click="page++">下一页</button></div></section>
</template>

<style>
.runs-table td small{display:block;font-size:10px;line-height:1.8;color:#91a095}.runs-table td.mono{overflow-wrap:anywhere}.table-scroll{overflow-x:auto;max-width:100%}
</style>
