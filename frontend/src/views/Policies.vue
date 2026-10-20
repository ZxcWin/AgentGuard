<script setup lang="ts">
import { computed, nextTick, ref, toRaw, watch } from 'vue'
import { Coins, Layers3, Gauge, ShieldCheck, Check, RefreshCw, Save, Info, LoaderCircle, Clock3, Wrench } from 'lucide-vue-next'
import { api, ApiError, type Policy, type PolicyConfig, type PolicyValue } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'
import { authRequired, principal } from '../session'
import { health } from '../state'
const persistentCache = computed(() => health.value?.cache_backend === 'sqlite')
const readOnly = computed(() => authRequired.value && principal.value?.role !== 'admin')
const { data, loading, error, reload } = useResource(signal => api<Policy>('/policies/current', { signal }))
const form = ref<PolicyConfig>({}), version = ref(1), notice = ref(''), saving = ref(false), validated = ref(false), conflict = ref(false)
const mappings = ref({ per_tool_limits: '{}', side_effects: '{}', idempotency_required_for: '[]', approval_required_for: '[]' })
const fallbackJsonFields = [
  { key: 'json_schema', group: 'quality', label: '响应 JSON Schema', array: false, help: 'Draft 2020-12；null 关闭，只允许本地 $ref，不加载远程引用' },
  { key: 'required_substrings', group: 'quality', label: '响应必需文本 JSON', array: true, help: '例如 ["结论"]；区分大小写，最多 32 条' },
  { key: 'model_profiles', group: 'degradation', label: '模型能力声明 JSON', array: false, help: '例如 {"cheap": {"capabilities": ["text", "tools", "json_schema"], "regions": ["cn"], "max_data_classification": "internal"}}' },
  { key: 'models', group: 'pricing', label: '模型价格 JSON', array: false, help: '每百万 Token 的 USD 价格，例如 {"cheap": {"input_per_million": 0.1, "output_per_million": 0.4}}' },
  { key: 'required_capabilities', group: 'degradation', label: '额外必需能力 JSON', array: true, help: '例如 ["tools"]；支持 text / tools / json_object / json_schema / vision / audio / streaming' },
  { key: 'allowed_regions', group: 'degradation', label: '允许地域 JSON', array: true, help: '例如 ["cn"]；所有可能的路由地域均需在此列表内，空列表不限制' },
] as const
const fallbackJson = ref<Record<string, string>>({})
function jsonFields(config: PolicyConfig) { return Object.fromEntries(fallbackJsonFields.map(field => [field.key, JSON.stringify(config[field.group]?.[field.key] ?? (field.key === 'json_schema' ? null : field.array ? [] : {}), null, 2)])) }
const noticeSuccess = ref(false)
const cacheModels = computed({
  get: () => (Array.isArray(form.value.cache?.allowed_models) ? form.value.cache.allowed_models : []).join('\n'),
  set: (value: string) => { form.value.cache!.allowed_models = value.split(/[\n,]/).map(model => model.trim()).filter(Boolean) },
})
const cacheTools = computed({
  get: () => (Array.isArray(form.value.cache?.allowed_tools) ? form.value.cache.allowed_tools : []).join('\n'),
  set: (value: string) => { form.value.cache!.allowed_tools = value.split(/[\n,]/).map(tool => tool.trim()).filter(Boolean) },
})
async function clearCache() {
  saving.value = true; notice.value = ''; noticeSuccess.value = false
  try { const result = await api<{ scope: string }>('/cache', { method: 'DELETE' }); noticeSuccess.value = true; notice.value = result.scope === 'shared_host' ? '共享 SQLite 缓存已清空，所有连接实例的旧条目均已失效。' : '当前服务进程的缓存已清空。' }
  catch (reason) { notice.value = reason instanceof Error ? reason.message : '清空失败' }
  finally { saving.value = false }
}
const securityFlags = [
  { key: 'detect_secrets', label: '检测密钥与访问令牌' },
  { key: 'detect_pii', label: '检测邮箱与中国大陆手机号' },
  { key: 'detect_prompt_injection', label: '检测请求中的提示注入规则' },
]
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
  { key: 'circuit', name: '故障熔断', description: '同一 Run 内按作用域隔离模型与工具故障；恢复期后仅放行一个探测调用。', icon: ShieldCheck, fields: [
    { key: 'failure_threshold', label: '连续失败阈值', help: '达到阈值后拒绝该作用域的新调用；成功后清零', unit: '次', min: 1, max: 1000000 },
    { key: 'recovery_seconds', label: '熔断恢复等待时间', help: '打开后等待此时长再探测；0 表示立即允许单个探测', unit: '秒', min: 0, max: 86400, step: 0.001 },
    { key: 'max_keys', label: '熔断作用域容量', help: '每个 Run 的作用域数量上限，超出时拒绝新增作用域', unit: '个', min: 1, max: 100000 },
  ] },
  { key: 'tools', name: '工具治理', description: '在执行前检查总次数、步数与重复参数，限制工具副作用。', icon: Wrench, fields: [
    { key: 'max_calls', label: '每次运行工具调用上限', help: '所有工具共享的总执行次数上限', unit: '次' },
    { key: 'max_steps', label: '最大工具步数', help: '一次 Run 内允许执行的工具步骤', unit: '步' },
    { key: 'max_repeated_calls', label: '相同工具与参数重复上限', help: '按函数签名规范化参数，在重复执行前阻断', unit: '次' },
    { key: 'max_depth', label: '工具递归深度上限', help: '在下一层工具执行前阻断，0 禁止工具', unit: '层' },
    { key: 'max_concurrent', label: '同一 Run 并行工具上限', help: '包含执行中和等待返回的工具', unit: '个' },
    { key: 'approval_timeout_seconds', label: '审批等待上限', help: '异步审批最长等待时间；留空继承工具或 Run 截止时间', unit: '秒', step: 0.001 },
  ] },
  { key: 'behavior', name: '进展与循环检测', description: '仅存输出摘要散列；达到上限后阻止下一次外部调用。', icon: ShieldCheck, fields: [
    { key: 'max_repeated_outputs', label: '连续相同输出上限', help: '模型或工具连续返回相同文本的次数', unit: '次' },
    { key: 'max_calls_without_progress', label: '无进展调用上限', help: '应用通过 mark_progress 报告新里程碑，同一标记不能重置', unit: '次' },
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
function selectProfile() {
  if (form.value.enforcement?.profile !== 'production') return
  Object.assign(form.value.token!, { missing_price: 'reject', missing_usage: 'estimate' })
  Object.assign(form.value.security!, { enabled: true, action: 'block', detect_secrets: true })
}
async function submit(save: boolean) {
  saving.value = true; notice.value = ''; conflict.value = false; noticeSuccess.value = false
  try {
    for (const field of fallbackJsonFields) {
      let parsed: unknown
      try { parsed = JSON.parse(fallbackJson.value[field.key] || '') } catch { throw new Error(`${field.label} 格式不正确。`) }
      if (field.key === 'json_schema' ? parsed !== null && typeof parsed !== 'boolean' && (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) : field.array ? !Array.isArray(parsed) : !parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(`${field.label} 格式不符合要求。`)
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
  <div v-if="readOnly" class="info-banner">当前角色仅可查看策略。发布配置需要管理员权限。</div>
  <form @submit.prevent="submit(true)"><fieldset :disabled="readOnly" style="border:0;padding:0;margin:0;min-width:0">
  <section v-if="form.enforcement" class="card policy-card">
    <div class="policy-heading"><span class="policy-icon"><ShieldCheck :size="20"/></span><div><h2>执行配置</h2><p>严格模式会校验预算、定价与安全设置，保存后对新 Run 生效。</p></div></div>
    <div class="policy-selects"><label>配置模式<select v-model="form.enforcement.profile" :disabled="saving" @change="selectProfile"><option value="development">开发模式</option><option value="production">生产严格模式</option></select></label>
    <label>模型价格缺失行为<select v-model="form.token!.missing_price" :disabled="saving"><option value="observe">记录事件并继续</option><option value="reject">调用前拒绝</option></select></label>
    <label>Usage 超出预估行为<select v-model="form.token!.usage_mismatch" :disabled="saving"><option value="observe">记录实际用量并继续</option><option value="reject">结算实际用量并拒绝响应</option></select></label></div>
    <p class="privacy-note">选择严格模式会启用密钥阻断、缺价拒绝和缺失用量估算。请填写输出上限、Run Token 与成本预算及各模型完整价格；不支持通配价格。跨实例额度需在 SDK 接入共享预算存储；控制台策略写入使用可靠审计。切回开发模式保留已设置的防护。</p>
  </section><section v-for="group in groups" :key="group.key" class="card policy-card"><div class="policy-heading"><span class="policy-icon"><component :is="group.icon" :size="20"/></span><div><h2>{{ group.name }}</h2><p>{{ group.description }}</p></div><span class="subtle-tag">已支持</span></div><div class="policy-fields"><label v-for="field in group.fields" :key="field.key" class="policy-field"><strong>{{ field.label }}</strong><small>{{ field.help }}</small><span class="number-input"><input type="number" :value="form[group.key]?.[field.key] ?? ''" :min="'min' in field ? field.min : 0" :required="group.key === 'circuit'" :max="'max' in field ? field.max : undefined" :step="'step' in field ? field.step : 1" placeholder="不限制" :disabled="saving" @input="setNumber(group.key, field.key, $event)"/><span>{{ field.unit }}</span></span></label></div><div v-if="group.key === 'token'" class="policy-selects"><label>预算超限行为<select v-model="form.token!.on_exceeded" :disabled="saving"><option value="reject">拒绝调用</option><option value="stop_stream">停止流式输出</option><option value="downgrade">降级（需预先配置 fallback）</option></select></label><label>Usage 缺失行为<select v-model="form.token!.missing_usage" :disabled="saving"><option value="estimate">使用估算并标记来源</option><option value="unknown">标记为未知</option><option value="reject">拒绝响应（保守结算）</option></select></label></div><div v-if="group.key === 'retry' && form.retry" class="policy-selects"><label><input v-model="form.retry.jitter" type="checkbox" :disabled="saving"/>开启随机抖动</label><p>失败请求的未知用量保守计入预算；已执行子工具的调用不重试。</p></div></section>
  <section class="card policy-mappings"><div class="card-heading"><div><h2>单工具规则</h2><p>工具名与 invoke_tool 的名称一致；不记录工具参数或输出原文。</p></div></div><div class="policy-fields"><label class="policy-field"><strong>单工具额度 JSON</strong><small>例如 {"search": 10, "write_file": 1}</small><textarea v-model="mappings.per_tool_limits" :disabled="saving" rows="5" spellcheck="false" aria-label="单工具额度 JSON"></textarea></label><label class="policy-field"><strong>工具副作用 JSON</strong><small>read / write / irreversible，默认 write</small><textarea v-model="mappings.side_effects" :disabled="saving" rows="5" spellcheck="false" aria-label="工具副作用 JSON"></textarea></label><label class="policy-field"><strong>必须带幂等键的工具 JSON</strong><small>例如 ["write_file", "charge_card"]；重复键会在副作用前阻断</small><textarea v-model="mappings.idempotency_required_for" :disabled="saving" rows="5" spellcheck="false" aria-label="必须带幂等键的工具 JSON"></textarea></label><label class="policy-field"><strong>必须审批的工具 JSON</strong><small>例如 ["charge_card"]；调用时传入 approval 回调</small><textarea v-model="mappings.approval_required_for" :disabled="saving" rows="5" spellcheck="false" aria-label="必须审批的工具 JSON"></textarea></label></div></section>
  <section v-if="form.degradation && form.pricing" class="card policy-card policy-mappings">
    <div class="policy-heading"><span class="policy-icon"><Layers3 :size="20"/></span><div><h2>模型降级约束</h2><p>预算不足时在同一供应商入口切换模型；能力、地域、数据等级和价格检查通过才执行。</p></div><span class="subtle-tag">已支持</span></div>
    <div class="policy-fields">
      <label class="policy-field"><strong>备用模型</strong><small>需同时声明模型能力和价格；留空关闭降级</small><span class="number-input"><input type="text" :value="form.degradation.fallback_model ?? ''" :disabled="saving" placeholder="例如 cheap" @input="form.degradation.fallback_model = ($event.target as HTMLInputElement).value.trim() || null"/></span></label>
      <label class="policy-field"><strong>降级调用成本上限</strong><small>单次降级调用的预估 USD 上限，仍受父 Run 与重试预算限制</small><span class="number-input"><input type="number" :value="form.degradation.max_cost_usd ?? ''" min="0" step="0.000001" :disabled="saving" placeholder="不限制" @input="setNumber('degradation', 'max_cost_usd', $event)"/><span>USD</span></span></label>
      <label class="policy-field"><strong>请求数据等级</strong><small>备用模型声明的最高等级必须覆盖当前请求等级</small><select v-model="form.degradation.data_classification" :disabled="saving"><option value="public">公开 public</option><option value="internal">内部 internal</option><option value="confidential">机密 confidential</option><option value="restricted">受限 restricted</option></select></label>
      <label class="policy-field"><strong>价格表版本</strong><small>与模型价格同时保存，供后续运行快照使用</small><span class="number-input"><input v-model="form.pricing.version" type="text" :disabled="saving"/></span></label>
      <label v-for="field in fallbackJsonFields.filter(f => f.group !== 'quality')" :key="field.key" class="policy-field"><strong>{{ field.label }}</strong><small>{{ field.help }}</small><textarea v-model="fallbackJson[field.key]" :disabled="saving" rows="5" spellcheck="false" :aria-label="field.label"></textarea></label>
    </div>
    <p class="privacy-note">在上方将预算超限行为设为“降级”后启用。工具、结构化输出与媒体需求按请求结构检查；供应商特有格式请补充额外必需能力。声明描述部署入口，SDK 不验证供应商实际路由。工具审批由调用方传入 approval 回调，服务端负责超时和脱敏审计。</p>
  </section>
  <section v-if="form.security" class="card policy-card">
    <div class="policy-heading"><span class="policy-icon"><ShieldCheck :size="20"/></span><div><h2>内容安全</h2><p>在请求发送前、响应返回前和流式消费时检查内容，事件只保留规则代码。</p></div><span class="subtle-tag">已支持</span></div>
    <div class="policy-selects">
      <label><input v-model="form.security.enabled" type="checkbox" :disabled="saving"/>启用内容安全</label>
      <label>安全命中行为<select v-model="form.security.action" :disabled="saving"><option value="block">阻断内容</option><option value="observe">仅记录事件</option><option value="redact">脱敏后继续（非流式）</option></select></label>
      <label v-for="flag in securityFlags" :key="flag.key"><input v-model="form.security[flag.key]" type="checkbox" :disabled="saving"/>{{ flag.label }}</label>
      <label>安全扫描字符上限<input type="number" :value="form.security.max_chars" min="1" max="2000000" required :disabled="saving" @input="setNumber('security', 'max_chars', $event)"/></label>
    </div>
    <p class="privacy-note">规则检测覆盖常见密钥、凭据字段、邮箱、手机号及英文提示注入特征。脱敏保留 JSON 结构，以 HMAC 标记替换命中值，不记录原文；提示注入、无法安全转换或超出扫描上限时阻断。脱敏仅支持非流式文本和普通 JSON，启用脱敏时拒绝流式请求。检测器异常会阻断。严格生产模式要求阻断行为。</p>
  </section>
  <section v-if="form.quality" class="card policy-card">
    <div class="policy-heading"><span class="policy-icon"><Check :size="20"/></span><div><h2>响应质量</h2><p>验证返回内容格式，SDK 支持注册自定义质量评估器。</p></div><span class="subtle-tag">已支持</span></div>
    <div class="policy-selects">
      <label><input v-model="form.quality.enabled" type="checkbox" :disabled="saving"/>启用质量检查</label>
      <label><input v-model="form.quality.strict_json" type="checkbox" :disabled="saving"/>要求严格 JSON</label>
      <label>质量失败行为<select v-model="form.quality.action" :disabled="saving"><option value="block">拒绝响应</option><option value="observe">仅记录事件</option><option value="retry">预算内重试</option><option value="repair">修复请求后重试</option><option value="degrade">切换备用模型</option><option value="human">人工批准后重试</option></select></label>
      <label>质量重试次数<input type="number" :value="form.quality.max_retries" min="0" max="10" required :disabled="saving" @input="setNumber('quality', 'max_retries', $event)"/></label>
      <label>修复与人工处理时限（秒）<input type="number" :value="form.quality.handler_timeout_seconds" min="0.001" max="300" step="0.001" required :disabled="saving" @input="setNumber('quality', 'handler_timeout_seconds', $event)"/></label>
      <label>最少响应字符<input type="number" :value="form.quality.min_chars" min="0" required :disabled="saving" @input="setNumber('quality', 'min_chars', $event)"/></label>
      <label>质量评估字符上限<input type="number" :value="form.quality.max_chars" min="1" max="2000000" required :disabled="saving" @input="setNumber('quality', 'max_chars', $event)"/></label>
    </div>
    <div class="policy-fields"><label v-for="field in fallbackJsonFields.filter(f => f.group === 'quality')" :key="field.key" class="policy-field"><strong>{{ field.label }}</strong><small>{{ field.help }}</small><textarea v-model="fallbackJson[field.key]" :disabled="saving" rows="5" spellcheck="false" :aria-label="field.label"></textarea></label></div>
    <p class="privacy-note">重试、修复、备用模型和人工处理共用质量重试次数、父预算与重试额度。修复需应用注册请求修复回调；人工处理需审批回调及可靠审计，未接入时拒绝。备用模型沿用能力、地区、数据等级和价格限制，每次调用最多切换一次。人工只批准一个新请求，仍需通过所有检查。流式及已执行子工具的调用不重放；已发出的片段无法撤回。评估器异常会阻断。</p>
  </section>
  <section v-if="form.cache" class="card policy-card">
    <div class="policy-heading"><span class="policy-icon"><Layers3 :size="20"/></span><div><h2>精确缓存</h2><p>默认关闭，复用已明确授权的公开或内部纯模型响应与只读工具结果。</p></div><span class="subtle-tag">{{ persistentCache ? 'SQLite 持久化' : '本地内存' }}</span></div>
    <div class="policy-selects"><label><input v-model="form.cache.enabled" type="checkbox" :disabled="saving"/>启用精确缓存</label>
      <label>缓存有效期（秒）<input type="number" :value="form.cache.ttl_seconds" min="1" max="3600" required :disabled="saving" @input="setNumber('cache', 'ttl_seconds', $event)"/></label>
      <label>缓存条目上限<input type="number" :value="form.cache.max_entries" min="1" max="10000" required :disabled="saving" @input="setNumber('cache', 'max_entries', $event)"/></label>
      <label>缓存容量上限（字节）<input type="number" :value="form.cache.max_bytes" min="1" max="268435456" required :disabled="saving" @input="setNumber('cache', 'max_bytes', $event)"/></label>
    </div>
    <div class="policy-fields"><label class="policy-field"><strong>允许缓存的模型</strong><small>每行一个模型，或使用英文逗号分隔；不支持通配符</small><textarea v-model="cacheModels" :disabled="saving" rows="3" aria-label="允许缓存的模型"></textarea></label><label class="policy-field"><strong>允许缓存的只读工具</strong><small>每行一个工具；工具副作用还必须设为 read</small><textarea v-model="cacheTools" :disabled="saving" rows="3" aria-label="允许缓存的只读工具"></textarea></label></div>
    <p class="privacy-note">模型调用需声明纯调用、数据等级、授权及 Prompt 版本；只读工具另需提供工具代码和数据版本。均须提供租户、项目、环境和用户。敏感内容、机密/受限数据、写入或不可逆工具、审批和幂等工具、流式响应及失败结果不缓存；工具命中仍执行限流、配额、熔断和审计。缓存命中不增加供应商调用次数、Token 或成本。{{ persistentCache ? '当前使用同机共享 SQLite；重启复用还需相同签名密钥和明确的供应商身份，清空会使所有连接实例失效。' : '重启会清空本地缓存。可在启动服务时配置 SQLite 缓存与独立密钥文件。' }}</p>
    <button type="button" class="button" :disabled="saving" @click="clearCache"><RefreshCw :size="15"/>{{ persistentCache ? '清空共享 SQLite 缓存' : '清空当前进程缓存' }}</button>
  </section>
  <section v-if="form.telemetry" class="card policy-card">
    <div class="policy-heading"><span class="policy-icon"><Info :size="20"/></span><div><h2>遥测与保留</h2><p>普通运行记录按结束时间清理；运行中的记录保留。</p></div></div>
    <div class="policy-selects"><label><input v-model="form.telemetry.enable_metrics_export" type="checkbox" :disabled="saving"/>启用 Prometheus 指标接口</label><label>记录保留天数<input type="number" :value="form.telemetry.retention_days" min="1" max="30" required :disabled="saving" @input="setNumber('telemetry', 'retention_days', $event)"/></label></div>
    <p class="privacy-note">本地服务每分钟清理过期记录。缩短保留期会删除到期数据；普通遥测不提供持久审计保证。</p>
  </section>
  <section class="planned-card"><ShieldCheck :size="20"/><div><strong>更多治理能力</strong><p>语义缓存和跨实例审批工作流仍在规划中。熔断状态仅在当前 Run 内维护，不跨 Run 或跨实例共享。</p></div><span class="subtle-tag">部分规划中</span></section>
  <div v-if="notice" class="form-notice" :class="noticeSuccess ? 'success' : 'warning'" role="status"><Check v-if="validated" :size="17"/><Info v-else :size="17"/><span>{{ notice }}</span><button v-if="conflict" type="button" class="text-link" @click="reload">加载最新版本</button></div>
  <div class="policy-save-bar"><span>{{ dirty ? '有尚未保存的更改' : '当前配置已与服务同步' }}<small>重试参数必填；其他上限留空不限制，0 表示零额度。</small></span><div><button type="button" class="button" :disabled="saving" @click="submit(false)"><ShieldCheck :size="15"/>校验配置</button><button type="submit" class="button primary" :disabled="saving || !dirty"><LoaderCircle v-if="saving" :size="15" class="spin"/><Save v-else :size="15"/>{{ saving ? '正在保存…' : '保存策略' }}</button></div></div></fieldset></form></template>
</template>
