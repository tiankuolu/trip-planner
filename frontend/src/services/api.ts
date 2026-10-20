import axios from 'axios'
import type {
  TaskDetail,
  TaskSubmitResponse,
  TripFormData,
  TripPlanResponse
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''  // 空字符串走Vite代理，避免CORS

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

function toError(error: any, fallback: string): Error {
  if (error.code === 'ERR_NETWORK' || error.message === 'Network Error') {
    return new Error('无法连接到服务器，请确认后端已启动(端口8000)')
  }
  return new Error(error.response?.data?.detail || error.message || fallback)
}

/**
 * 提交生成任务（异步）：立刻拿到 task_id，不再等 20 秒
 */
export async function submitTripPlan(
  formData: TripFormData,
  idempotencyKey?: string
): Promise<TaskSubmitResponse> {
  try {
    const response = await apiClient.post<TaskSubmitResponse>('/api/trip/plan', formData, {
      headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined
    })
    return response.data
  } catch (error: any) {
    throw toError(error, '提交任务失败')
  }
}

/**
 * 查询任务状态
 */
export async function getTask(taskId: string): Promise<TaskDetail> {
  try {
    const response = await apiClient.get<TaskDetail>(`/api/trip/tasks/${taskId}`)
    return response.data
  } catch (error: any) {
    throw toError(error, '查询任务失败')
  }
}

/**
 * 取消任务
 */
export async function cancelTask(taskId: string): Promise<TaskDetail> {
  try {
    const response = await apiClient.post<TaskDetail>(`/api/trip/tasks/${taskId}/cancel`)
    return response.data
  } catch (error: any) {
    throw toError(error, '取消任务失败')
  }
}

const TERMINAL_STATUS = ['succeeded', 'failed', 'canceled']

/**
 * 轮询直到任务结束；每次轮询回调一次，SSE 不可用时作为降级方案
 */
export async function waitForTask(
  taskId: string,
  onUpdate?: (task: TaskDetail) => void,
  intervalMs = 1000,
  timeoutMs = 240000
): Promise<TaskDetail> {
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    const task = await getTask(taskId)
    onUpdate?.(task)
    if (TERMINAL_STATUS.includes(task.status)) {
      return task
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }

  throw new Error('任务超时（4 分钟），可稍后到任务列表查看')
}

/**
 * 任务列表
 */
export async function listTasks(status?: string, limit = 20): Promise<TaskDetail[]> {
  try {
    const response = await apiClient.get<TaskDetail[]>('/api/trip/tasks', {
      params: { status: status || undefined, limit }
    })
    return response.data
  } catch (error: any) {
    throw toError(error, '查询任务列表失败')
  }
}

const SSE_EVENTS = [
  'task.created',
  'task.running',
  'task.resumed',
  'task.step',
  'task.cancel_requested',
  'task.succeeded',
  'task.failed',
  'task.canceled',
  'done',
  'timeout',
  'error'
]

/**
 * 订阅任务事件流（SSE）
 *
 * 返回取消订阅的函数。连接异常时交给调用方决定是否降级成轮询。
 */
export function subscribeTaskEvents(
  taskId: string,
  handlers: {
    onEvent?: (type: string, data: any) => void
    onDone?: (status: string) => void
    onError?: () => void
  }
): () => void {
  const source = new EventSource(`${API_BASE_URL}/api/trip/tasks/${taskId}/events`)

  for (const type of SSE_EVENTS) {
    source.addEventListener(type, (event: MessageEvent) => {
      let data: any = null
      try {
        data = JSON.parse(event.data)
      } catch {
        data = { raw: event.data }
      }
      handlers.onEvent?.(type, data)
      if (type === 'done') {
        handlers.onDone?.(data?.status || 'unknown')
        source.close()
      }
    })
  }

  source.onerror = () => {
    // 服务端会主动 close（正常结束），此时 readyState 是 CLOSED
    if (source.readyState === EventSource.CLOSED) return
    handlers.onError?.()
  }

  return () => source.close()
}

/**
 * 兼容旧调用：提交 + 等待，返回与原来一致的 TripPlanResponse 结构
 */
export async function generateTripPlan(
  formData: TripFormData,
  onUpdate?: (task: TaskDetail) => void
): Promise<TripPlanResponse> {
  const submitted = await submitTripPlan(formData, makeIdempotencyKey(formData))
  const task = await waitForTask(submitted.task_id, onUpdate)

  if (task.status !== 'succeeded' || !task.plan) {
    const reason = task.error?.message || `任务状态: ${task.status}`
    throw new Error(reason)
  }

  return {
    success: true,
    message: task.degraded ? `生成成功（${task.warnings.length} 项数据降级）` : '生成成功',
    data: task.plan,
    degraded: task.degraded,
    warnings: task.warnings,
    data_sources: task.data_sources,
    elapsed_ms: task.elapsed_ms,
    task_id: task.task_id
  }
}

/**
 * 幂等键：同一份表单 10 分钟内重复点击不会重复生成
 */
export function makeIdempotencyKey(formData: TripFormData): string {
  const raw = [
    formData.city,
    formData.start_date,
    formData.end_date,
    formData.travel_days,
    formData.transportation,
    formData.accommodation,
    (formData.preferences || []).join(','),
    formData.free_text_input
  ].join('|')
  const bucket = Math.floor(Date.now() / (10 * 60 * 1000))
  let hash = 0
  const text = `${raw}#${bucket}`
  for (let i = 0; i < text.length; i++) {
    hash = (hash * 31 + text.charCodeAt(i)) | 0
  }
  return `trip-${Math.abs(hash).toString(36)}`
}

export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    throw toError(error, '健康检查失败')
  }
}

export default apiClient
