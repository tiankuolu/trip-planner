<template>
  <div class="tasks-container">
    <div class="page-header">
      <a-space>
        <a-button @click="goHome">← 返回首页</a-button>
      </a-space>
      <a-space>
        <a-radio-group v-model:value="filter" button-style="solid" @change="load">
          <a-radio-button value="">全部</a-radio-button>
          <a-radio-button value="running">执行中</a-radio-button>
          <a-radio-button value="succeeded">已完成</a-radio-button>
          <a-radio-button value="failed">失败</a-radio-button>
        </a-radio-group>
        <a-button :loading="loading" @click="load">刷新</a-button>
      </a-space>
    </div>

    <a-card :bordered="false" class="table-card">
      <template #title>
        任务列表
        <span class="count">共 {{ tasks.length }} 条</span>
      </template>

      <a-table
        :data-source="tasks"
        :columns="columns"
        :pagination="{ pageSize: 10 }"
        :loading="loading"
        row-key="task_id"
        size="middle"
        :row-class-name="() => 'clickable-row'"
        @row-click="(record: TaskDetail) => openTask(record)"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'task_id'">
            <span class="mono">{{ record.task_id }}</span>
          </template>
          <template v-else-if="column.key === 'status'">
            <a-tag :color="STATUS_COLOR[record.status]">{{ STATUS_LABEL[record.status] }}</a-tag>
          </template>
          <template v-else-if="column.key === 'trip'">
            {{ record.city }} · {{ record.travel_days }} 天
          </template>
          <template v-else-if="column.key === 'date'">
            {{ record.start_date }} ~ {{ record.end_date }}
          </template>
          <template v-else-if="column.key === 'steps'">
            {{ record.steps.length }}
            <a-tag v-if="record.degraded" color="orange">降级</a-tag>
          </template>
          <template v-else-if="column.key === 'elapsed'">
            {{ record.elapsed_ms ? (record.elapsed_ms / 1000).toFixed(1) + 's' : '-' }}
          </template>
          <template v-else-if="column.key === 'error'">
            <span v-if="record.error" class="error-text">{{ record.error.code }}</span>
            <span v-else class="muted">-</span>
          </template>
          <template v-else-if="column.key === 'created'">
            {{ formatTime(record.created_at) }}
          </template>
        </template>
        <template #emptyText>
          <a-empty description="还没有任务，回首页提交一个吧" />
        </template>
      </a-table>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { listTasks } from '@/services/api'
import type { TaskDetail } from '@/types'

const router = useRouter()
const tasks = ref<TaskDetail[]>([])
const loading = ref(false)
const filter = ref('')

let refreshTimer: number | null = null

const STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '执行中',
  succeeded: '已完成',
  failed: '失败',
  canceled: '已取消'
}
const STATUS_COLOR: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
  canceled: 'warning'
}

const columns = [
  { title: '任务', key: 'task_id', width: 220 },
  { title: '状态', key: 'status', width: 110 },
  { title: '目的地', key: 'trip', width: 140 },
  { title: '日期', key: 'date', width: 200 },
  { title: '步骤', key: 'steps', width: 100 },
  { title: '耗时', key: 'elapsed', width: 100 },
  { title: '失败原因', key: 'error', width: 150 },
  { title: '创建时间', key: 'created', width: 170 }
]

function formatTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  try {
    tasks.value = await listTasks(filter.value || undefined)
  } catch (error: any) {
    message.error(error.message || '加载任务列表失败')
  } finally {
    loading.value = false
  }
}

function openTask(record: TaskDetail) {
  router.push(`/task/${record.task_id}`)
}

const goHome = () => router.push('/')

onMounted(async () => {
  await load()
  // 列表页也保持自动刷新：有人正在跑任务时能看到状态自己变化
  refreshTimer = window.setInterval(load, 4000)
})

onBeforeUnmount(() => {
  if (refreshTimer !== null) window.clearInterval(refreshTimer)
})
</script>

<style scoped>
.tasks-container {
  max-width: 1240px;
  margin: 0 auto;
  padding: 24px 16px 60px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 18px;
  gap: 12px;
  flex-wrap: wrap;
}

.table-card {
  border-radius: 16px;
  box-shadow: 0 8px 30px rgba(102, 126, 234, 0.12);
}

.count {
  margin-left: 10px;
  color: #999;
  font-size: 13px;
  font-weight: 400;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 13px;
}

.muted {
  color: #bbb;
}

.error-text {
  color: #cf1322;
  font-size: 13px;
}

:deep(.clickable-row) {
  cursor: pointer;
}
</style>
