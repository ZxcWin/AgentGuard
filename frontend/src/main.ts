import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Overview from './views/Overview.vue'
import Runs from './views/Runs.vue'
import Policies from './views/Policies.vue'
import Settings from './views/Settings.vue'
import './style.css'

const router = createRouter({ history: createWebHashHistory(), routes: [
  { path: '/', component: Overview, meta: { title: '运行概览', label: 'Overview' } },
  { path: '/runs', component: Runs, meta: { title: '运行记录', label: 'Runs' } },
  { path: '/policies', component: Policies, meta: { title: '防护策略', label: 'Policies' } },
  { path: '/settings', component: Settings, meta: { title: '接入设置', label: 'Settings' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
] })
createApp(App).use(router).mount('#app')
