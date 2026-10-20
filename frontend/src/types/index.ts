// 类型定义

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  /** 后端已回填高德坐标；极端情况下可能为 null（会被后端过滤掉） */
  location?: Location | null
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
  /** amap=高德回填的真实数据，llm=模型给出未校验 */
  source?: 'amap' | 'llm'
  poi_id?: string
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
  /** amap=高德回填的真实数据，llm=模型给出未校验 */
  source?: 'amap' | 'llm'
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface DayRoute {
  distance: number
  duration: number
  route_type: string
  description: string
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  hotel_brand: string
  hotel_location: string
  hotel_location_detail: string
  preferences: string[]
  free_text_input: string
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan
  /** 有步骤降级（数据没取到 / 被替换）时为 true */
  degraded?: boolean
  warnings?: string[]
  data_sources?: Record<string, number>
  elapsed_ms?: number
  task_id?: string
}

// ============ 任务化接口 ============

export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled'

export interface TaskStepView {
  step: string
  status: string
  tool?: string | null
  arguments?: Record<string, unknown> | null
  item_count?: number | null
  latency_ms: number
  mode?: string | null
  detail?: string | null
}

export interface TaskSubmitResponse {
  task_id: string
  status: TaskStatus
  status_url: string
  events_url: string
  deduplicated: boolean
}

export interface TaskDetail {
  task_id: string
  status: TaskStatus
  city: string
  start_date: string
  end_date: string
  travel_days: number
  degraded: boolean
  warnings: string[]
  data_sources: Record<string, number>
  error?: { code?: string; message?: string } | null
  attempt_count: number
  elapsed_ms: number
  token_usage: number
  created_at?: string | null
  started_at?: string | null
  ended_at?: string | null
  steps: TaskStepView[]
  plan?: TripPlan
}

