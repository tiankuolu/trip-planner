<template>
  <div class="task-container">
    <div class="page-header">
      <a-space>
        <a-button @click="goHome">← 返回首页</a-button>
        <a-button @click="goTasks">📋 任务列表</a-button>
      </a-space>
      <a-space>
        <a-tag :color="statusColor" class="status-tag">{{ statusLabel }}</a-tag>
        <a-button
          v-if="task?.status === 'succeeded' && task.plan"
          type="primary"
          @click="viewPlan"
        >
          查看行程
        </a-button>
        <a-button v-if="canCancel" danger :loading="canceling" @click="onCancel">取消任务</a-button>
      </a-space>
    </div>

    <a-card class="status-card" :bordered="false">
      <div class="status-line">
        <span class="task-id">任务 {{ taskId }}</span>
        <span v-if="task" class="meta">
          {{ task.city }} · {{ task.start_date }} ~ {{ task.end_date }} · {{ task.travel_days }} 天
        </span>
        <span class="timer">{{ elapsedText }}</span>
      </div>

      <a-progress
        :percent="progress"
        :status="progressStatus"
        :stroke-color="{ '0%': '#667eea', '100%': '#764ba2' }"
        :stroke-width="10"
      />
      <p class="hint">{{ hint }}</p>

      <a-alert
        v-if="task?.error"
        type="error"
        show-icon
        :message="`失败：${task.error.code || '未知错误'}`"
        :description="task.error.message || ''"
      />
      <a-alert
        v-else-if="task?.degraded && task.warnings.length"
        type="warning"
        show-icon
        message="部分数据降级"
        :description="task.warnings.join('；')"
      />

      <div v-if="task" class="stats">
        <span>步骤 {{ steps.length }}</span>
        <span>尝试 {{ task.attempt_count }}</span>
        <span>token {{ task.token_usage }}</span>
        <span>耗时 {{ (task.elapsed_ms / 1000).toFixed(1) }}s</span>
      </div>
    </a-card>

    <a-card title="步骤时间线" :bordered="false" class="timeline-card">
      <a-timeline>
        <a-timeline-item
          v-for="(step, index) in steps"
          :key="`${index}-${step.step}`"
          :color="step.status === 'failed' ? 'red' : 'green'"
        >
          <div class="step-title">
            {{ step.step }}
            <a-tag v-if="step.tool" color="blue">{{ step.tool }}</a-tag>
            <a-tag v-if="step.mode" color="purple">{{ step.mode }}</a-tag>
          </div>
          <div class="step-detail">
            <span v-if="step.item_count !== null && step.item_count !== undefined">
              {{ step.item_count }} 条
            </span>
            <span>{{ step.latency_ms }} ms</span>
            <span v-if="step.detail">{{ step.detail }}</span>
          </div>
        </a-timeline-item>
        <a-timeline-item v-if="!steps.length" color="gray">
          等待第一步执行...
        </a-timeline-item>
      </a-timeline>
    </a-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { cancelTask, getTask, subscribeTaskEvents } from '@/services/api'
import type { TaskDetail, TaskStepView } from '@/types'

const route = useRoute()
const router = useRouter()
const taskId = route.params.id as string

const task = ref<TaskDetail | null>(null)
const steps = ref<TaskStepView[]>([])
const canceling = ref(false)
const now = ref(Date.now())
const streamState = ref<'connecting' | 'live' | 'fallback'>('connecting')
// 只有"这次访问期间跑完的"才自动跳转；打开历史任务时应当留在时间线页
const autoRedirect = ref(false)

let closeStream: (() => void) | null = null
let pollTimer: number | null = null
let tickTimer: number | null = null

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

const statusLabel = computed(() => STATUS_LABEL[task.value?.status || 'pending'] || '未知')
const statusColor = computed(() => STATUS_COLOR[task.value?.status || 'pending'] || 'default')
const terminal = computed(() =>
  ['succeeded', 'failed', 'canceled'].includes(task.value?.status || '')
)
const canCancel = computed(() => !terminal.value && !canceling.value)

const progress = computed(() => {
  const status = task.value?.status || 'pending'
  if (status === 'succeeded') return 100
  if (status === 'pending') return 8
  if (status === 'failed' || status === 'canceled') return Math.min(95, 15 + steps.value.length * 18)
  return Math.min(95, 15 + steps.value.length * 18)
})

const progressStatus = computed(() => {
  const status = task.value?.status
  if (status === 'succeeded') return 'success'
  if (status === 'failed') return 'exception'
  return 'active'
})

const elapsedText = computed(() => {
  if (!task.value) return ''
  if (terminal.value && task.value.elapsed_ms) return `${(task.value.elapsed_ms / 1000).toFixed(1)}s`
  const started = task.value.started_at ? Date.parse(task.value.started_at) : Date.now()
  const seconds = Math.max(0, (now.value - started) / 1000)
  return `已运行 ${seconds.toFixed(0)}s`
})

const hint = computed(() => {
  const status = task.value?.status
  if (status === 'pending') return '已进入队列，等待 worker 领取（队列模式）或后台开始执行'
  if (status === 'running') {
    const last = steps.value[steps.value.length - 1]
    return last ? `正在执行：${last.step}` : '正在执行...'
  }
  if (status === 'succeeded') return autoRedirect.value ? '生成完成，即将跳转到行程页' : '生成完成'
  if (status === 'failed') return '任务失败，失败原因见下方'
  if (status === 'canceled') return '任务已取消'
  return streamState.value === 'fallback' ? 'SSE 不可用，已降级为轮询' : '正在连接事件流...'
})

function stashPlan(detail: TaskDetail) {
  if (!detail.plan) return
  sessionStorage.setItem('tripPlan', JSON.stringify(detail.plan))
  sessionStorage.setItem(
    'tripPlanMeta',
    JSON.stringify({
      degraded: detail.degraded,
      warnings: detail.warnings,
      data_sources: detail.data_sources,
      elapsed_ms: detail.elapsed_ms,
      task_id: detail.task_id
    })
  )
}

function viewPlan() {
  if (!task.value) return
  stashPlan(task.value)
  router.push('/result')
}

function applyTask(detail: TaskDetail) {
  task.value = detail
  if (detail.steps?.length) steps.value = detail.steps

  if (detail.status === 'succeeded' && detail.plan) {
    stashPlan(detail)
    stopWatching()
    // 本次访问期间跑完的：自动进入行程页；历史任务：停在时间线，用户自己点
    if (autoRedirect.value) {
      setTimeout(() => router.push('/result'), 700)
    }
  }
  if (['failed', 'canceled'].includes(detail.status)) stopWatching()
}

async function refresh() {
  try {
    applyTask(await getTask(taskId))
  } catch (error: any) {
    message.error(error.message || '查询任务失败')
    stopWatching()
  }
}

function startFallbackPolling() {
  if (pollTimer !== null) return
  streamState.value = 'fallback'
  pollTimer = window.setInterval(refresh, 1500)
}

function startWatching() {
  autoRedirect.value = true
  closeStream = subscribeTaskEvents(taskId, {
    onEvent: (type, data) => {
      streamState.value = 'live'
      if (type === 'task.step' && data?.payload) {
        const payload = data.payload
        steps.value = [
          ...steps.value,
          {
            step: payload.step,
            status: payload.status || 'succeeded',
            tool: payload.tool || null,
            item_count: payload.item_count ?? null,
            latency_ms: payload.latency_ms || 0,
            mode: null,
            detail: null
          }
        ]
      }
      if (type.startsWith('task.') && type !== 'task.step') {
        // 状态事件：直接拉一次详情，保证 warnings/error/plan 都是权威值
        refresh()
      }
    },
    onDone: () => {
      refresh()
    },
    onError: () => {
      startFallbackPolling()
    }
  })
}

function stopWatching() {
  closeStream?.()
  closeStream = null
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

async function onCancel() {
  canceling.value = true
  try {
    await cancelTask(taskId)
    message.info('已请求取消，正在等待执行器退出')
    await refresh()
  } catch (error: any) {
    message.error(error.message || '取消失败')
  } finally {
    canceling.value = false
  }
}

const goHome = () => router.push('/')
const goTasks = () => router.push('/tasks')

onMounted(async () => {
  tickTimer = window.setInterval(() => {
    now.value = Date.now()
  }, 500)

  await refresh()
  if (!terminal.value) startWatching()
})

onBeforeUnmount(() => {
  stopWatching()
  if (tickTimer !== null) window.clearInterval(tickTimer)
})
</script>

<style scoped>
.task-container {
  max-width: 1000px;
  margin: 0 auto;
  padding: 24px 16px 60px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 18px;
}

.status-card {
  border-radius: 16px;
  box-shadow: 0 8px 30px rgba(102, 126, 234, 0.12);
  margin-bottom: 18px;
}

.status-line {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.task-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 15px;
  color: #444;
}

.meta {
  color: #666;
  font-size: 14px;
}

.timer {
  margin-left: auto;
  color: #764ba2;
  font-weight: 600;
}

.hint {
  color: #666;
  margin: 4px 0 12px;
}

.stats {
  display: flex;
  gap: 18px;
  margin-top: 12px;
  color: #888;
  font-size: 13px;
}

.timeline-card {
  border-radius: 16px;
}

.step-title {
  font-weight: 600;
  color: #333;
  display: flex;
  align-items: center;
  gap: 8px;
}

.step-detail {
  color: #888;
  font-size: 13px;
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
}
</style>
