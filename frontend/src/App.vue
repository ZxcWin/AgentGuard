<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ShieldCheck, LayoutDashboard, ListTree, SlidersHorizontal, PlugZap, ChevronDown, ArrowUpRight, CircleHelp, PanelLeftClose, RefreshCw, Menu, X, CircleDot, Coins } from 'lucide-vue-next'
import { api, apiDocsUrl } from './api'
import { authRequired, principal, workspaceToken } from './session'
import { useModal } from './useModal'
import { checkHealth, health, connectionError, refreshKey } from './state'
const route = useRoute()
const nav = [{ path: '/', name: '运行概览', icon: LayoutDashboard }, { path: '/runs', name: '运行记录', icon: ListTree }, { path: '/policies', name: '防护策略', icon: SlidersHorizontal }, { path: '/audit', name: '审计记录', icon: ShieldCheck }, { path: '/settings', name: '接入设置', icon: PlugZap }]
const tokenInput = ref(''), loginError = ref(''), signingIn = ref(false)
nav.splice(4, 0, { path: '/billing', name: '成本账本', icon: Coins })
const visibleNav = computed(() => nav.filter(item => !authRequired.value || principal.value?.role === 'admin' || !['/audit', '/billing'].includes(item.path)))
async function login() {
  signingIn.value = true; loginError.value = ''; workspaceToken.value = tokenInput.value.trim()
  try { principal.value = await api('/session'); tokenInput.value = ''; refreshKey.value++ }
  catch { workspaceToken.value = ''; loginError.value = '凭证无效或服务不可用，请检查后重试。' }
  finally { signingIn.value = false }
}
const menu = ref(false)
const help = ref(false)
const helpTrigger = ref<HTMLElement | null>(null)
const helpPanel = ref<HTMLElement | null>(null)
const helpKeydown = useModal(helpPanel, help, () => { help.value = false }, helpTrigger)
const navPanel = ref<HTMLElement | null>(null)
const navTrigger = ref<HTMLElement | null>(null)
const navKeydown = useModal(navPanel, menu, () => { menu.value = false }, navTrigger)
let timer: ReturnType<typeof setInterval>
onMounted(() => { checkHealth(); timer = setInterval(checkHealth, 30000) })
onUnmounted(() => clearInterval(timer))
async function refresh() { await checkHealth(); refreshKey.value++ }
</script>
<template>
  <div class="app-shell" :inert="authRequired && !principal">
    <button v-if="menu" class="nav-backdrop" aria-label="关闭导航" @click="menu = false"></button>
    <aside ref="navPanel" class="sidebar" :inert="help" :class="{ open: menu }" @keydown="navKeydown">
      <RouterLink class="brand" to="/"><span class="brand-symbol"><ShieldCheck :size="23"/></span><span>AgentGuard<span class="brand-edition">CONSOLE</span></span></RouterLink>
      <RouterLink to="/settings" class="workspace" @click="menu = false"><span class="workspace-avatar">AG</span><span><strong>本地工作空间</strong><small>Local workspace</small></span><ChevronDown :size="15"/></RouterLink>
      <div class="nav-label">工作空间</div>
      <nav><RouterLink v-for="item in visibleNav" :key="item.path" :to="item.path" class="nav-item" :class="{ active: route.path === item.path }" @click="menu = false"><component :is="item.icon" :size="18"/><span>{{ item.name }}</span><span v-if="route.path === item.path" class="active-dot"></span></RouterLink></nav>
      <div class="sidebar-bottom"><div class="runtime-note"><span class="small-shield"><ShieldCheck :size="19"/></span><strong>运行时防护</strong><span class="status-dot" :class="{ offline: !health }"></span><small>{{ health ? '本地服务已连接' : '等待服务连接' }}</small></div><button ref="helpTrigger" class="support-link" @click="help = true"><CircleHelp :size="17"/>文档与帮助<ArrowUpRight :size="14"/></button><div class="sidebar-footer"><span class="avatar">L</span><span>本地开发环境<small>SDK v{{ health?.sdk_version || '0.1.0' }}</small></span><PanelLeftClose :size="16"/></div></div>
    </aside>
    <div class="main-shell" :inert="help || menu"><header class="topbar"><div class="breadcrumbs"><button ref="navTrigger" class="icon-button mobile-menu" title="展开导航" @click="menu = true"><Menu :size="20"/></button><span>工作空间</span><span class="breadcrumb-slash">/</span><strong>{{ route.meta.title }}</strong></div><div class="topbar-right"><span class="connection"><span class="status-dot" :class="{ offline: !health }"></span>{{ health ? '服务在线' : '连接断开' }}</span><span class="top-divider"></span><span class="environment"><CircleDot :size="12"/>本地环境</span><span class="avatar">L</span></div></header>
      <div v-if="health?.demo" class="demo-banner"><span><span class="status-dot"></span>演示环境 · 本地示例数据</span><RouterLink to="/settings">查看连接信息<ArrowUpRight :size="13"/></RouterLink></div>
      <div v-if="connectionError" class="connection-banner" role="alert">无法连接 AgentGuard 服务：{{ connectionError }}<button @click="refresh"><RefreshCw :size="14"/>重新连接</button></div>
      <main><RouterView :key="route.path"/></main>
      <footer class="page-footer"><span><ShieldCheck :size="13"/>AgentGuard <span class="muted">/</span> Runtime governance for AI agents</span><span>本地工作空间 · API v{{ health?.api_version || '1' }}</span></footer>
    </div>
    <div v-if="help" class="modal-overlay" @click.self="help = false"><section ref="helpPanel" class="help-modal" @keydown="helpKeydown" role="dialog" aria-modal="true" aria-labelledby="help-title"><button class="icon-button modal-close" title="关闭" @click="help = false"><X :size="18"/></button><ShieldCheck :size="30" class="green"/><h2 id="help-title">AgentGuard 文档</h2><p>项目根目录的 README.md 包含安装、SDK 接入和运行说明。控制台接口定义可在本地服务的 API 文档中查看。</p><a class="button primary" :href="apiDocsUrl" target="_blank" rel="noreferrer">打开 API 文档<ArrowUpRight :size="15"/></a></section></div>
  </div>
  <div v-if="authRequired && !principal" class="modal-overlay"><section class="help-modal" role="dialog" aria-modal="true" aria-labelledby="login-title"><ShieldCheck :size="30" class="green"/><h2 id="login-title">连接工作空间</h2><p>输入服务管理员分配的访问凭证。凭证仅保存在当前页面内存中。</p><form @submit.prevent="login"><label class="policy-field"><strong>工作空间访问凭证</strong><input v-model="tokenInput" type="password" autocomplete="off" required :disabled="signingIn"/></label><p v-if="loginError" role="alert" class="danger">{{ loginError }}</p><button class="button primary" :disabled="signingIn">{{ signingIn ? '正在验证…' : '连接' }}</button></form></section></div>
</template>
