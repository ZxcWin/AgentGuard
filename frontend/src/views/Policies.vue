<script setup lang="ts">
import { computed, nextTick, ref, toRaw, watch } from 'vue'
import { Coins, Layers3, Gauge, ShieldCheck, Check, RefreshCw, Save, Info, LoaderCircle, Clock3, Wrench } from 'lucide-vue-next'
import { api, ApiError, type Policy, type PolicyConfig, type PolicyValue } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'
const { data, loading, error, reload } = useResource(signal => api<Policy>('/policies/current', { signal }))
const form = ref<PolicyConfig>({}), version = ref(1), notice = ref(''), saving = ref(false), validated = ref(false), conflict = ref(false)
const mappings = ref({ per_tool_limits: '{}', side_effects: '{}', idempotency_required_for: '[]', approval_required_for: '[]' })
const fallbackJsonFields = [
  { key: 'model_profiles', group: 'degradation', label: '模型能力声明 JSON', array: false, help: '例如 {"cheap": {"capabilities": ["text", "tools", "json_schema"], "regions": ["cn"], "max_data_classification": "internal"}}' },
  { key: 'models', group: 'pricing', label: '模型价格 JSON', array: false, help: '每百万 Token 的 USD 价格，例如 {"cheap": {"input_per_million": 0.1, "output_per_million": 0.4}}' },
  { key: 'required_capabilities', group: 'degradation', label: '额外必需能力 JSON', array: true, help: '例如 ["tools"]；支持 text / tools / json_object / json_schema / vision / audio / streaming' },
  { key: 'allowed_regions', group: 'degradation', label: '允许地域 JSON', array: true, help: '例如 ["cn"]；所有可能的路由地域均需在此列表内，空列表不限制' },
] as const
const fallbackJson = ref<Record<string, string>>({})
function jsonFields(config: PolicyConfig) { return Object.fromEntries(fallbackJsonFields.map(field => [field.key, JSON.stringify(config[field.group]?.[field.key] ?? (field.array ? [] : {}), null, 2)])) }
const noticeSuccess = ref(false)
const groups = [
  { key: 'token', name: 'Token 预算', description: '为单次调用与整次运行设置清晰的消耗边界。', icon: Layers3, fields: [
    { key: 'run_token_budget', label: '每次运行 Token 上限', help: '一次 Run 内所有模型调用的总 Token 预算', unit: 'tokens' },
    { key: 'max_input_tokens', label: '单次输入 Token 上限', help: '请求发出前检查输入大小', unit: 'tokens' },
    { key: 'max_output_tokens', label: '单次输出 Token 上限', help: '保留用户指定的更严格限制', unit: 'tokens' },
    { key: 'reservation_buffer', label: '预留缓冲比例', help: '0 到 1 之间，为估算偏差预留空间', unit: '比例', max: 1, step: 0.01 },
    { key: 'retry_budget_ratio', label: '重试预算比例', help: '重试、质量重试和降级共用父 Run 预算的比例', unit: '比例', max: 1, step: 0.01 },
  ] },
  { key: 'budget', name: '成本预算', description: '依据配置的模型价格，在调用前预留并在完成后结算。', icon: Coins, fields: [
    { key: 'run_cost_usd', label: '每次运行成本上限', help: '对整次 Agent 运行实施成本约束', unit: 'USD', step: 0.01 },
    { key: 'max_cost_usd', label: '单次调用成本上限', help: '限制一次模型请求的预估成本', unit: 'USD', step: 0.01 },
    { key: 'cost_warning_threshold', label: '成本告警阈值', help: '0 到 1 之间，达到预算比例时记录告警事件', unit: '比例', max: 1, step: 0.01 },
  ] },
  { key: 'rate_limit', name: '限流与工具调用', description: '控制当前 Run 的调用频率、并发和工具调用次数。', icon: Gauge, fields: [
    { key: 'rpm', label: '每分钟请求上限', help: '按配置的 scope key 限制请求', unit: 'RPM' },
    { key: 'tpm', label: '每分钟 Token 上限', help: '按估算 Token 准入', unit: 'TPM' },
    { key: 'max_concurrency', label: '最大并发调用', help: '单个 scope 同时执行的调用数量', unit: '个' },
    { key: 'max_tool_calls', label: '工具调用上限', help: '单个工具 scope 在窗口内的调用次数', unit: '次' },
  ] },
  { key: 'tools', name: '工具治理', description: '在执行前检查总次数、步数与重复参数，限制工具副作用。', icon: Wrench, fields: [
    { key: 'max_calls', label: '每次运行工具调用上限', help: '所有工具共享的总执行次数上限', unit: '次' },
    { key: 'max_steps', label: '最大工具步数', help: '一次 Run 内允许执行的工具步骤', unit: '步' },
    { key: 'max_repeated_calls', label: '相同工具与参数重复上限', help: '按函数签名规范化参数，在重复执行前阻断', unit: '次' },
    { key: 'approval_timeout_seconds', label: '审批等待上限', help: '异步审批最长等待时间；留空继承工具或 Run 截止时间', unit: '秒', step: 0.001 },
  ] },
  { key: 'timeout', name: '执行超时', description: '子调用继承更短的截止时间；同步调用在返回后检查，异步调用协作取消。', icon: Clock3, fields: [
    { key: 'run_seconds', label: 'Run 总耗时上限', help: '从进入 Run 开始计算，不延长父 Run 的时限', unit: '秒', step: 0.001 },
    { key: 'llm_seconds', label: '模型调用耗时上限', help: '涵盖响应等待与流式消费；包含较短的父时限', unit: '秒', step: 0.001 },
    { key: 'tool_seconds', label: '工具调用耗时上限', help: '副作用工具超时后可能已执行，不自动重试', unit: '秒', step: 0.001 },
  ] },
  { key: 'retry', name: '模型重试', description: '默认关闭，仅重试连接错误、供应商超时、408/429 和部分 5xx；流和工具不自动重试。', icon: RefreshCw, fields: [
    { key: 'max_retries', label: '最大重试次数', help: '0 表示关闭；不含首次请求，最多 10 次', unit: '次', max: 10 },
    { key: 'initial_delay_seconds', label: '初始退避时间', help: '每次失败后指数递增，不超过最大退避时间', unit: '秒', step: 0.001 },
    { key: 'max_delay_seconds', label: '最大退避时间', help: 'Retry-After 超过此值时停止，不提前重试', unit: '秒', step: 0.001 },
    { key: 'max_elapsed_seconds', label: '重试总耗时上限', help: '包含首次请求、等待和所有重试；必须大于 0', unit: '秒', step: 0.001 },
  ] },
]
watch(data, value => { if (value) { form.value = structuredClone(toRaw(value.config)); fallbackJson.value = jsonFields(value.config); version.value = value.version; notice.value = ''; conflict.value = false; validated.value = false; mappings.value = { per_tool_limits: JSON.stringify(value.config.tools?.per_tool_limits || {}, null, 2), side_effects: JSON.stringify(value.config.tools?.side_effects || {}, null, 2), idempotency_required_for: JSON.stringify(value.config.tools?.idempotency_required_for || [], null, 2), approval_required_for: JSON.stringify(value.config.tools?.approval_required_for || [], null, 2) } } })
watch(form, () => { validated.value = false }, { deep: true })
watch([mappings, fallbackJson], () => { validated.value = false }, { deep: true })
const dirty = computed(() => JSON.stringify(form.value) !== JSON.stringify(data.value?.config) || JSON.stringify(fallbackJson.value) !== JSON.stringify(jsonFields(data.value?.config || {})) || JSON.stringify(mappings.value) !== JSON.stringify({ per_tool_limits: JSON.stringify(data.value?.config.tools?.per_tool_limits || {}, null, 2), side_effects: JSON.stringify(data.value?.config.tools?.side_effects || {}, null, 2), idempotency_required_for: JSON.stringify(data.value?.config.tools?.idempotency_required_for || [], null, 2), approval_required_for: JSON.stringify(data.value?.config.tools?.approval_required_for || [], null, 2) }))
function setNumber(group: string, key: string, event: Event) { const value = (event.target as HTMLInputElement).value; form.value[group]![key] = value === '' ? null : Number(value) }
async function submit(save: boolean) {
  saving.value = true; notice.value = ''; conflict.value = false; noticeSuccess.value = false
  try {
    for (const field of fallbackJsonFields) {
      let parsed: unknown
      try { parsed = JSON.parse(fallbackJson.value[field.key] || '') } catch { throw new Error(`${field.label} 格式不正确。`) }
      if (field.array ? !Array.isArray(parsed) : !parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${field.label} 必须是 JSON ${field.array ? '数组' : '对象'}。`)
      form.value[field.group]![field.key] = parsed as PolicyValue
    }
    for (const key of ['per_tool_limits', 'side_effects', 'idempotency_required_for', 'approval_required_for'] as const) {
      let parsed: unknown
      try { parsed = JSON.parse(mappings.value[key]) } catch { throw new Error(`${key} 必须是合法 JSON 对象。`) }
      if (!parsed || typeof parsed !== 'object' || (key.includes('required_for') ? !Array.isArray(parsed) : Array.isArray(parsed))) throw new Error(`${key} 必须是 JSON ${key.includes('required_for') ? '数组' : '对象'}。`)
      form.value.tools![key] = parsed as PolicyValue
    }
    const body = JSON.stringify({ config: form.value, expected_version: version.value })
    await api('/policies/validate', { method: 'POST', body })
    if (save) { data.value = await api<Policy>('/policies/current', { method: 'PUT', body }); await nextTick(); notice.value = '策略已保存。新 Run 将使用新版本，已有 Run 保持原快照。' }
    else { validated.value = true; notice.value = '校验通过，可以保存当前配置。' }
    noticeSuccess.value = true
  } catch (reason) { conflict.value = reason instanceof ApiError && reason.status === 409; notice.value = conflict.value ? '策略版本已被其他操作更新。请重新加载最新策略后编辑。' : reason instanceof Error ? reason.message : '操作失败' }
  finally { saving.value = false }
}
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">RUNTIME POLICIES</div><h1>防护策略<span v-if="data" class="version-tag">v{{ version }}</span></h1><p>定义 Agent 的运行边界，让每次执行遵循同一份策略快照。</p></div><button class="button" :disabled="loading || saving" @click="reload"><RefreshCw :size="15"/>重新加载</button></div>
  <ResourceState :loading="loading" :error="error" @retry="reload"/>
  <template v-if="data && !loading && !error"><div class="info-banner"><Info :size="17"/><span>策略作用于当前连接服务。配置持久保存在 SQLite，重启保留版本；已有 Run 继续使用创建时的版本。</span></div>
  <form @submit.prevent="submit(true)"><section v-for="group in groups" :key="group.key" class="card policy-card"><div class="policy-heading"><span class="policy-icon"><component :is="group.icon" :size="20"/></span><div><h2>{{ group.name }}</h2><p>{{ group.description }}</p></div><span class="subtle-tag">已支持</span></div><div class="policy-fields"><label v-for="field in group.fields" :key="field.key" class="policy-field"><strong>{{ field.label }}</strong><small>{{ field.help }}</small><span class="number-input"><input type="number" :value="form[group.key]?.[field.key] ?? ''" min="0" :max="'max' in field ? field.max : undefined" :step="'step' in field ? field.step : 1" placeholder="不限制" :disabled="saving" @input="setNumber(group.key, field.key, $event)"/><span>{{ field.unit }}</span></span></label></div><div v-if="group.key === 'token'" class="policy-selects"><label>预算超限行为<select v-model="form.token!.on_exceeded" :disabled="saving"><option value="reject">拒绝调用</option><option value="stop_stream">停止流式输出</option><option value="downgrade">降级（需预先配置 fallback）</option></select></label><label>Usage 缺失行为<select v-model="form.token!.missing_usage" :disabled="saving"><option value="estimate">使用估算并标记来源</option><option value="unknown">标记为未知</option><option value="reject">拒绝结算</option></select></label></div><div v-if="group.key === 'retry' && form.retry" class="policy-selects"><label><input v-model="form.retry.jitter" type="checkbox" :disabled="saving"/>开启随机抖动</label><p>失败请求的未知用量保守计入预算；已执行子工具的调用不重试。</p></div></section>
  <section class="card policy-mappings"><div class="card-heading"><div><h2>单工具规则</h2><p>工具名与 invoke_tool 的名称一致；不记录工具参数或输出原文。</p></div></div><div class="policy-fields"><label class="policy-field"><strong>单工具额度 JSON</strong><small>例如 {"search": 10, "write_file": 1}</small><textarea v-model="mappings.per_tool_limits" :disabled="saving" rows="5" spellcheck="false" aria-label="单工具额度 JSON"></textarea></label><label class="policy-field"><strong>工具副作用 JSON</strong><small>read / write / irreversible，默认 write</small><textarea v-model="mappings.side_effects" :disabled="saving" rows="5" spellcheck="false" aria-label="工具副作用 JSON"></textarea></label><label class="policy-field"><strong>必须带幂等键的工具 JSON</strong><small>例如 ["write_file", "charge_card"]；重复键会在副作用前阻断</small><textarea v-model="mappings.idempotency_required_for" :disabled="saving" rows="5" spellcheck="false" aria-label="必须带幂等键的工具 JSON"></textarea></label><label class="policy-field"><strong>必须审批的工具 JSON</strong><small>例如 ["charge_card"]；调用时传入 approval 回调</small><textarea v-model="mappings.approval_required_for" :disabled="saving" rows="5" spellcheck="false" aria-label="必须审批的工具 JSON"></textarea></label></div></section>
  <section v-if="form.degradation && form.pricing" class="card policy-card policy-mappings">
    <div class="policy-heading"><span class="policy-icon"><Layers3 :size="20"/></span><div><h2>模型降级约束</h2><p>预算不足时在同一供应商入口切换模型；能力、地域、数据等级和价格检查通过才执行。</p></div><span class="subtle-tag">已支持</span></div>
    <div class="policy-fields">
      <label class="policy-field"><strong>备用模型</strong><small>需同时声明模型能力和价格；留空关闭降级</small><span class="number-input"><input type="text" :value="form.degradation.fallback_model ?? ''" :disabled="saving" placeholder="例如 cheap" @input="form.degradation.fallback_model = ($event.target as HTMLInputElement).value.trim() || null"/></span></label>
      <label class="policy-field"><strong>降级调用成本上限</strong><small>单次降级调用的预估 USD 上限，仍受父 Run 与重试预算限制</small><span class="number-input"><input type="number" :value="form.degradation.max_cost_usd ?? ''" min="0" step="0.000001" :disabled="saving" placeholder="不限制" @input="setNumber('degradation', 'max_cost_usd', $event)"/><span>USD</span></span></label>
      <label class="policy-field"><strong>请求数据等级</strong><small>备用模型声明的最高等级必须覆盖当前请求等级</small><select v-model="form.degradation.data_classification" :disabled="saving"><option value="public">公开 public</option><option value="internal">内部 internal</option><option value="confidential">机密 confidential</option><option value="restricted">受限 restricted</option></select></label>
      <label class="policy-field"><strong>价格表版本</strong><small>与模型价格同时保存，供后续运行快照使用</small><span class="number-input"><input v-model="form.pricing.version" type="text" :disabled="saving"/></span></label>
      <label v-for="field in fallbackJsonFields" :key="field.key" class="policy-field"><strong>{{ field.label }}</strong><small>{{ field.help }}</small><textarea v-model="fallbackJson[field.key]" :disabled="saving" rows="5" spellcheck="false" :aria-label="field.label"></textarea></label>
    </div>
    <p class="privacy-note">在上方将预算超限行为设为“降级”后启用。工具、结构化输出与媒体需求按请求结构检查；供应商特有格式请补充额外必需能力。声明描述部署入口，SDK 不验证供应商实际路由。工具审批由调用方传入 approval 回调，服务端负责超时和脱敏审计。</p>
  </section>
  <section class="planned-card"><ShieldCheck :size="20"/><div><strong>更多治理能力</strong><p>熔断参数编辑与跨实例审批工作流仍在规划中；当前工具幂等和审批门禁已可用。</p></div><span class="subtle-tag">部分规划中</span></section>
  <div v-if="notice" class="form-notice" :class="noticeSuccess ? 'success' : 'warning'" role="status"><Check v-if="validated" :size="17"/><Info v-else :size="17"/><span>{{ notice }}</span><button v-if="conflict" type="button" class="text-link" @click="reload">加载最新版本</button></div>
  <div class="policy-save-bar"><span>{{ dirty ? '有尚未保存的更改' : '当前配置已与服务同步' }}<small>重试参数必填；其他上限留空不限制，0 表示零额度。</small></span><div><button type="button" class="button" :disabled="saving" @click="submit(false)"><ShieldCheck :size="15"/>校验配置</button><button type="submit" class="button primary" :disabled="saving || !dirty"><LoaderCircle v-if="saving" :size="15" class="spin"/><Save v-else :size="15"/>{{ saving ? '正在保存…' : '保存策略' }}</button></div></div></form></template>
</template>
