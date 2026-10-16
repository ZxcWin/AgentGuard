<script setup lang="ts">
import { computed, ref } from 'vue'
const props = defineProps<{ series: { timestamp: string; calls: number; tokens: number; failed: number }[]; metric: 'calls' | 'tokens' }>()
const hover = ref(-1)
const max = computed(() => Math.max(5, ...props.series.map(row => row[props.metric])))
const points = computed(() => props.series.map((row, index) => `${32 + index * 920 / 23},${191 - row[props.metric] / max.value * 153}`).join(' '))
const ticks = computed(() => [0, 6, 12, 18, 23].map(index => ({ x: 32 + index * 920 / 23, text: new Date(props.series[index]?.timestamp || Date.now()).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }) })))
</script>
<template>
  <div class="activity-chart" @mouseleave="hover = -1">
    <svg viewBox="0 0 980 235" role="img" :aria-label="metric === 'calls' ? '模型调用趋势' : 'Token 消耗趋势'" preserveAspectRatio="none">
      <g v-for="n in 4" :key="n"><line x1="32" x2="954" :y1="38 + (n - 1) * 51" :y2="38 + (n - 1) * 51" stroke="#e8eceb" stroke-dasharray="3 5"/><text x="0" :y="42 + (n - 1) * 51" fill="#8b9592" font-size="11">{{ Math.round(max * (4 - n) / 3) }}</text></g>
      <polygon :points="`32,191 ${points} 952,191`" fill="#eaf5f0"/>
      <polyline :points="points" fill="none" stroke="#248b6c" stroke-width="2.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <text v-for="tick in ticks" :key="tick.x" :x="tick.x" y="224" text-anchor="middle" fill="#8b9592" font-size="11">{{ tick.text }}</text>
      <rect v-for="(row, index) in series" :key="row.timestamp" :x="14 + index * 920 / 23" y="25" width="40" height="170" fill="transparent" @mouseenter="hover = index"/>
      <circle v-if="hover >= 0 && series[hover]" :cx="32 + hover * 920 / 23" :cy="191 - series[hover][metric] / max * 153" r="4" fill="#248b6c" stroke="white" stroke-width="2"/>
    </svg>
    <span v-if="hover >= 0 && series[hover]" class="chart-tooltip">{{ new Date(series[hover].timestamp).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }} · {{ series[hover][metric] }} {{ metric === 'calls' ? '次调用' : 'Tokens' }}</span>
  </div>
</template>
