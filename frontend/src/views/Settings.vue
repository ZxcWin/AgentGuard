<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { PlugZap, RefreshCw, ShieldCheck, Copy, Check, ExternalLink, Terminal, Database, Info, Radio, Trash2 } from 'lucide-vue-next'
import { health, checkHealth, connectionError } from '../state'
import { api, apiBase, apiDocsUrl } from '../api'
import { useResource } from '../useResource'
import ResourceState from '../components/ResourceState.vue'
import { authRequired, clearSession, principal } from '../session'
interface Telemetry { received: number; exported: number; failed: number; dropped: number; sampled_out: number; batch_size: number; queued: number; capacity: number; retention_days: number; cleanup: { deleted_runs: number; failures: number } }
const telemetry = useResource(signal => api<Telemetry>('/telemetry', { signal }))
const copied = ref(false), testing = ref(false), copyError = ref('')
const proxyProjects = ref<{ name: string; upstream: string }[]>([])
const projectName = ref(''), upstreamUrl = ref('')
const proxyBase = `${window.location.origin}/proxy`
const proxyMessage = ref('')
onMounted(async () => {
  try { proxyProjects.value = (await api<{ items: { name: string; upstream: string }[] }>('/proxy/projects')).items }
  catch (reason) { proxyMessage.value = reason instanceof Error ? reason.message : '无法读取项目代理配置。' }
})
async function addProxyProject() {
  proxyMessage.value = ''
  const name = projectName.value.trim(), upstream = upstreamUrl.value.trim().replace(/\/$/, '')
  if (!/^[a-zA-Z0-9_.-]{1,64}$/.test(name)) { proxyMessage.value = '项目名只能包含字母、数字、点、下划线和连字符。'; return }
  try { const url = new URL(upstream); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error() }
  catch { proxyMessage.value = '请输入有效的 HTTP(S) 模型服务地址，不要在地址中包含 API Key。'; return }
  try {
    await api('/proxy/projects', { method: 'PUT', body: JSON.stringify({ name, upstream }) })
    proxyProjects.value = [...proxyProjects.value.filter(item => item.name !== name), { name, upstream }]
    projectName.value = ''
  } catch (reason) { proxyMessage.value = reason instanceof Error ? reason.message : '保存代理配置失败。' }
}
async function removeProxyProject(name: string) { try { await api(`/proxy/projects/${encodeURIComponent(name)}`, { method: 'DELETE' }); proxyProjects.value = proxyProjects.value.filter(item => item.name !== name) } catch (reason) { proxyMessage.value = reason instanceof Error ? reason.message : '删除代理配置失败。' } }
async function copyProxy(url: string) { try { await navigator.clipboard.writeText(url); proxyMessage.value = '代理地址已复制。' } catch { proxyMessage.value = url } }
const snippet = `from agentguard import AgentGuard\nfrom agentguard.adapters.storage.sqlite import SQLiteRunStore\n\ndatabase = ".agentguard/runs.db"  # must match the console --database path\nstore = SQLiteRunStore(database)\nguard = AgentGuard(run_store=store)\n\ndef call_model(**request):\n    # Replace this body with your provider SDK call and return its response.\n    return my_client.responses.create(**request)\n\ntry:\n    with guard.run(name="my-agent", environment="local") as run:\n        result = run.invoke(\n            call_model,\n            model="your-model",\n            messages="Hello",\n            max_output_tokens=256,\n        )\nfinally:\n    guard.close()\n    store.close()`
async function copy() { try { await navigator.clipboard.writeText(snippet); copied.value = true; setTimeout(() => { copied.value = false }, 2000) } catch { copyError.value = '复制失败，请手动选择代码。' } }
async function test() { testing.value = true; await checkHealth(); testing.value = false }
</script>
<template>
  <div class="page-heading"><div><div class="eyebrow">CONNECT YOUR AGENTS</div><h1>接入设置</h1><p>连接本地服务，把 Agent 的运行状态带到眼前。</p></div><button class="button" :disabled="testing" @click="test"><RefreshCw :size="15" :class="{ spin: testing }"/>测试连接</button></div>
  <section class="card settings-connection"><div class="card-heading"><div><h2><PlugZap :size="18"/>服务连接</h2><p>控制台通过同源 HTTP 接口访问本地 AgentGuard 服务</p></div><span class="status-badge" :class="health ? 'completed' : 'failed'"><span class="tiny-dot"></span>{{ health ? '连接正常' : '连接断开' }}</span></div><dl class="connection-grid"><div><dt>API 地址</dt><dd class="mono">{{ apiBase }}</dd></div><div><dt>运行环境</dt><dd>{{ health?.demo ? '演示环境 · 本地示例数据' : '本地开发环境' }}</dd></div><div><dt>SDK 版本</dt><dd class="mono">{{ health?.sdk_version || '—' }}</dd></div><div><dt>API / Schema 版本</dt><dd class="mono">{{ health ? `v${health.api_version} / v${health.schema_version}` : '—' }}</dd></div><div><dt>运行记录存储</dt><dd><Database :size="14"/>{{ health?.storage || '—' }}</dd></div><div><dt>策略作用范围</dt><dd>当前本地数据库 · 持久配置</dd></div></dl><p v-if="connectionError" class="form-notice warning" role="alert">{{ connectionError }}</p></section>
  <div v-if="health?.demo" class="info-banner"><Info :size="17"/><span>当前已显式开启演示模式。退出演示时，去掉启动命令中的 --demo 参数；真实数据使用独立的 runs.db。</span></div>
  <section v-if="authRequired" class="card settings-connection"><div class="card-heading"><div><h2><ShieldCheck :size="18"/>工作空间访问</h2><p>当前身份 {{ principal?.subject }} · {{ principal?.role }}；服务端按角色授权。</p></div><button class="button" @click="clearSession">退出连接</button></div></section>
  <section class="card settings-connection"><div class="card-heading"><div><h2><Database :size="18"/>事件导出状态</h2><p>当前服务进程的普通遥测计数，重启后归零。</p></div><button class="button" :disabled="telemetry.loading.value" @click="telemetry.reload"><RefreshCw :size="15"/>刷新事件状态</button></div>
    <ResourceState :loading="telemetry.loading.value" :error="telemetry.error.value" @retry="telemetry.reload"/>
    <dl v-if="telemetry.data.value" class="connection-grid"><div><dt>已接收 / 已导出</dt><dd>{{ telemetry.data.value.received }} / {{ telemetry.data.value.exported }}</dd></div><div><dt>队列 / 容量</dt><dd>{{ telemetry.data.value.queued }} / {{ telemetry.data.value.capacity }}</dd></div><div><dt>导出失败 / 已丢弃</dt><dd>{{ telemetry.data.value.failed }} / {{ telemetry.data.value.dropped }}</dd></div><div><dt>普通记录保留期</dt><dd>{{ telemetry.data.value.retention_days }} 天</dd></div><div><dt>已清理运行记录</dt><dd>{{ telemetry.data.value.cleanup.deleted_runs }}</dd></div><div><dt>清理失败</dt><dd>{{ telemetry.data.value.cleanup.failures }}</dd></div></dl>
  </section>
  <p v-if="telemetry.data.value" class="privacy-note">普通事件按采样规则跳过 {{ telemetry.data.value.sampled_out || 0 }} 条；每批最多 {{ telemetry.data.value.batch_size || 64 }} 条。采样不会删减本地运行详情或必需审计。</p>
  <section class="card proxy-setup"><div class="card-heading"><div><h2><Radio :size="18"/>无代码模型代理</h2><p>适用于支持 OpenAI 兼容接口且可配置 Base URL 的项目。原 API Key 仍由你的 Agent 项目提供。</p></div><span class="subtle-tag">无需安装 SDK</span></div>
    <form class="proxy-form" @submit.prevent="addProxyProject"><label>项目名称<input v-model="projectName" aria-label="项目名称" placeholder="例如 patent-agent" maxlength="64"/></label><label>原模型服务地址<input v-model="upstreamUrl" aria-label="原模型服务地址" placeholder="例如 https://api.openai.com/v1" type="url"/></label><button class="button primary" type="submit"><Radio :size="15"/>生成代理地址</button></form>
    <p v-if="proxyMessage" class="proxy-message" role="status">{{ proxyMessage }}</p><p class="form-notice warning proxy-warning">先在本机运行 AgentGuard 控制台并打开本页面。配置保存在控制台数据库；每个项目仅需把 Base URL 改为生成的地址，原 API Key 保持不变。</p>
    <div v-for="project in proxyProjects" :key="project.name" class="proxy-project"><div><strong>{{ project.name }}</strong><small>转发目标：{{ project.upstream }}</small><code class="proxy-address">{{ proxyBase }}/{{ project.name }}/v1</code><p>回填旧项目的 Base URL；若项目不支持自定义 Base URL，则无法使用此免改代码方式。</p></div><div class="proxy-actions"><button class="icon-button" :title="'复制 '+project.name+' 代理地址'" @click="copyProxy(`${proxyBase}/${project.name}/v1`)"><Copy :size="15"/></button><button class="icon-button danger" :title="'删除 '+project.name+' 配置'" @click="removeProxyProject(project.name)"><Trash2 :size="15"/></button></div></div>
    <p class="proxy-disclosure">代理不会保存提示词或 API Key。当前仅转发并监控模型调用、Token 用量、耗时和错误；Agent 内部工具调用、非 OpenAI 兼容接口及部分特殊扩展不在监控范围。</p>
  </section>
  <section class="card integration-card"><div class="card-heading"><div><h2><Terminal :size="18"/>SDK 代码接入</h2><p>需要预算拦截、工具调用和单次任务调用树时使用完整 SDK</p></div><span class="subtle-tag">Python 3.10+</span></div><div class="integration-steps"><div class="integration-step"><span>1</span><div><h3>在模型调用和工具调用外建立 Run</h3><p>模型请求经 run.invoke 调用；Agent 工具经 run.invoke_tool 调用。</p><div class="code-block"><div><span>agent.py</span><button class="icon-button" :title="copied ? '已复制' : '复制接入代码'" @click="copy"><Check v-if="copied" :size="15"/><Copy v-else :size="15"/></button></div><pre>{{ snippet }}</pre></div><span v-if="copyError" role="alert" class="danger">{{ copyError }}</span></div></div></div></section>
  <section class="privacy-note settings-privacy"><ShieldCheck :size="23"/><div><strong>数据默认留在本地</strong><p>SQLite 仅保存运行摘要与允许字段内的事件。供应商密钥应由 Python 进程管理；前端只需访问本地 API。</p></div><a :href="apiDocsUrl" target="_blank" rel="noreferrer" class="text-link">API 文档<ExternalLink :size="14"/></a></section>
</template>
