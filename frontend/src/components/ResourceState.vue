<script setup lang="ts">
import { LoaderCircle, Unplug, Inbox, RefreshCw } from 'lucide-vue-next'
defineProps<{ loading?: boolean; error?: string; empty?: boolean; message?: string }>()
defineEmits<{ retry: [] }>()
</script>
<template>
  <div v-if="loading || error || empty" class="resource-state" :role="error ? 'alert' : 'status'">
    <LoaderCircle v-if="loading" :size="27" class="spin muted"/>
    <Unplug v-else-if="error" :size="29" class="muted"/>
    <Inbox v-else :size="30" class="muted"/>
    <strong>{{ loading ? '正在读取运行数据…' : error?.includes('无权访问') ? '无权访问' : error ? '暂时无法加载' : '还没有运行记录' }}</strong>
    <p v-if="!loading">{{ error || message || '接入 SDK 后，运行数据会显示在这里。' }}</p>
    <button v-if="error" class="button" @click="$emit('retry')"><RefreshCw :size="14"/>重新加载</button>
    <RouterLink v-else-if="empty && !loading" class="button" to="/settings">查看接入方式</RouterLink>
  </div>
</template>
