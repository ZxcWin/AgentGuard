<script setup lang="ts">
import { computed } from 'vue'
import { GitBranch, Wrench, Cpu, CornerDownRight } from 'lucide-vue-next'
import { statusName, type RunEvent } from '../api'
const props = defineProps<{ events: RunEvent[]; total: number }>()
interface CallNode { id: string; parent: string | null; name: string; type: string; status: string; duration: number | null; depth: number; attempt: number }
const calls = computed(() => {
  const nodes = new Map<string, CallNode>()
  for (const event of props.events) {
    if (!event.request_id) continue
    if (event.event === 'llm.requested' || event.event === 'tool.started') {
      nodes.set(event.request_id, { id: event.request_id, parent: typeof event.parent_id === 'string' ? event.parent_id : null,
        name: String(event.tool || event.model || '模型调用'), type: event.event.startsWith('tool') ? 'tool' : 'llm', status: 'running', duration: null, depth: 0, attempt: typeof event.attempt === 'number' ? event.attempt : 1 })
    }
    const node = nodes.get(event.request_id)
    if (node && /\.(completed|failed|timed_out)$/.test(event.event)) {
      node.status = typeof event.outcome === 'string' ? event.outcome : event.event.split('.').pop()!
      node.duration = typeof event.duration_seconds === 'number' ? event.duration_seconds * 1000 : null
    }
  }
  const result: CallNode[] = [], visited = new Set<string>()
  function visit(node: CallNode, depth: number) {
    if (visited.has(node.id)) return
    visited.add(node.id); result.push({ ...node, depth: Math.min(depth, 6) })
    for (const child of nodes.values()) if (child.parent === node.id) visit(child, depth + 1)
  }
  for (const node of nodes.values()) if (!node.parent || !nodes.has(node.parent)) visit(node, 0)
  for (const node of nodes.values()) if (!visited.has(node.id)) visit(node, 0)
  return result
})
</script>
<template>
  <section class="call-tree" aria-label="调用关系">
    <h3><GitBranch :size="17"/>调用关系<span>{{ calls.length }} 次调用</span></h3>
    <p v-if="!calls.length" class="muted">当前已加载事件中没有模型或工具调用。</p>
    <ul v-else><li v-for="call in calls" :key="call.id" :style="{ paddingLeft: 10 + call.depth * 13 + 'px' }">
      <CornerDownRight v-if="call.depth" :size="13" class="muted"/><Wrench v-if="call.type === 'tool'" :size="15"/><Cpu v-else :size="15"/>
      <div><strong>{{ call.name }}</strong><small v-if="call.attempt > 1">第 {{ call.attempt }} 次尝试</small><small class="mono">{{ call.id }}</small><small v-if="call.parent" class="mono muted">parent: {{ call.parent }}</small></div>
      <span class="status-badge" :class="call.status">{{ statusName(call.status) }}</span><small v-if="call.duration !== null" class="muted">{{ Math.round(call.duration) }} ms</small>
    </li></ul>
    <p v-if="events.length < total" class="muted">基于已加载的 {{ events.length }} / {{ total }} 个事件；加载更多事件可补全调用关系。</p>
  </section>
</template>
