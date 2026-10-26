import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, ArrowDownToLine, ArrowRight, ArrowUp, ArrowUpRight, CalendarDays, Check, ChevronRight, CircleAlert, Clock3, Compass, History, Hotel, Layers3, LoaderCircle, MapPin, Navigation, Plus, Send, Square, Sun, TrainFront, Wallet, Workflow, X } from 'lucide-react';
import Graph, { type GraphNode, type GraphEdge } from './Graph';
import TripMap, { type Place } from './Map';

type Day = { date: string; day_index: number; description: string; transportation: string; accommodation: string; attractions: Place[]; hotel?: { name: string; address: string; price_range: string; source?: string; estimated_cost?: number }; meals: { type: string; name: string; description: string; estimated_cost: number }[] };
type Plan = { city: string; start_date: string; end_date: string; days: Day[]; weather_info: { date: string; day_weather: string; night_weather: string; day_temp: number | string; night_temp: number | string; wind_direction: string; wind_power: string }[]; overall_suggestions: string; budget?: { total: number; total_attractions: number; total_hotels: number; total_meals: number; total_transportation: number } };
type Step = { step: string; status: string; tool?: string; arguments?: Record<string, unknown>; item_count?: number; latency_ms: number; mode?: string; detail?: string };
type Task = { task_id: string; status: string; city: string; start_date: string; end_date: string; travel_days: number; degraded: boolean; warnings: string[]; data_sources: Record<string, number>; error?: { code?: string; message?: string }; attempt_count: number; elapsed_ms: number; token_usage: number; created_at: string; steps: Step[]; plan?: Plan };
type Event = { seq: number; type: string; payload: Record<string, any> };
type Form = { city: string; start_date: string; end_date: string; transportation: string; accommodation: string; hotel_brand: string; hotel_location: string; hotel_location_detail: string; preferences: string[]; free_text_input: string };
const stateNames: Record<string, string> = { pending: '等待执行', running: '正在规划', succeeded: '规划完成', failed: '规划失败', canceled: '已取消' };
const eventNames: Record<string, string> = { 'task.created': '任务已创建', 'task.pending': '任务等待执行', 'task.running': '开始规划', 'task.resumed': '恢复任务执行', 'task.step': '规划步骤完成', 'task.succeeded': '规划完成', 'task.failed': '规划失败', 'task.canceled': '任务已取消', 'task.cancel_requested': '收到取消请求' };
const terminal = (status: string) => ['succeeded', 'failed', 'canceled'].includes(status);
const date = (time: number) => new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date(time));
const initialForm: Form = { city: '北京', start_date: date(Date.now() + 86400000), end_date: date(Date.now() + 3 * 86400000), transportation: '公共交通', accommodation: '舒适型酒店', hotel_brand: '', hotel_location: '景点附近', hotel_location_detail: '', preferences: ['历史文化', '美食'], free_text_input: '' };
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error(`服务请求失败（HTTP ${response.status}）`);
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
  return value;
}

function graphFor(task: Task | null, events: Event[], request: Record<string, any>, replaying: boolean): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (!task) {
    const nodes = ['读取旅行条件', '查询景点、天气与酒店', '编排行程并核验地点', '保存行程与执行记录'].map((title, index) => ({ id: `planned-${index}`, title, subtitle: '流程说明', status: 'planned', detail: { description: title } }));
    return { nodes, edges: nodes.slice(1).map((node, index) => ({ source: nodes[index].id, target: node.id })) };
  }
  const final = events.filter(event => ['task.succeeded', 'task.failed', 'task.canceled'].includes(event.type)).at(-1);
  const status = replaying ? final?.type.slice(5) || (events.some(event => ['task.running', 'task.resumed'].includes(event.type)) ? 'running' : 'pending') : task.status;
  const nodes: GraphNode[] = [{ id: 'root', title: `${task.city} · ${task.travel_days} 天旅行`, subtitle: `${task.start_date} 至 ${task.end_date}`, status, detail: { ...request, status, error: task.error } }];
  const edges: GraphEdge[] = [];
  const count = replaying ? events.filter(event => event.type === 'task.step').length : task.steps.length;
  task.steps.slice(0, count).forEach((step, index) => {
    nodes.push({ id: `step-${index}`, title: step.step, subtitle: `${step.tool || '模型编排'} · ${(step.latency_ms / 1000).toFixed(1)} 秒`, status: step.status, detail: step });
    edges.push({ source: index ? `step-${index - 1}` : 'root', target: `step-${index}` });
  });
  return { nodes, edges };
}

export default function App() {
  const [form, setForm] = useState<Form>(initialForm);
  const [config, setConfig] = useState({ map_key: '', map_security_code: '' });
  const [health, setHealth] = useState<Record<string, any> | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [task, setTask] = useState<Task | null>(null);
  const [request, setRequest] = useState<Record<string, any>>({});
  const [events, setEvents] = useState<Event[]>([]);
  const [tab, setTab] = useState('行程总览');
  const [dayIndex, setDayIndex] = useState(0);
  const [selectedPlace, setSelectedPlace] = useState<Place | null>(null);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [nodeTab, setNodeTab] = useState('执行详情');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [streamState, setStreamState] = useState('');
  const [playback, setPlayback] = useState<number | null>(null);
  const [editedPlan, setEditedPlan] = useState<Plan | null>(null);
  const version = useRef(0);
  const activeRef = useRef(task);
  activeRef.current = task;
  const lastSequence = useRef(0);
  const submission = useRef({ body: '', key: '' });

  useEffect(() => {
    Promise.all([api<typeof config>('/api/workbench/config'), api<Task[]>('/api/trip/tasks?limit=100'), api<Record<string, any>>('/api/trip/health')]).then(([settings, history, status]) => { setConfig(settings); setTasks(history); setHealth(status); }).catch(reason => setError(reason.message));
  }, []);
  async function refresh() { setTasks(await api<Task[]>('/api/trip/tasks?limit=100')); }
  async function load(id: string, reset = true) {
    const selection = reset ? ++version.current : version.current;
    const [detail, timeline, input] = await Promise.all([api<Task>(`/api/trip/tasks/${id}`), api<Event[]>(`/api/workbench/tasks/${id}/timeline`), api<Record<string, any>>(`/api/workbench/tasks/${id}/request`)]);
    if (selection !== version.current || (!reset && activeRef.current?.task_id !== id)) return;
    setTask(detail); setEvents(timeline); setRequest(input); lastSequence.current = timeline.at(-1)?.seq || 0;
    if (reset) {
      setTab(detail.status === 'succeeded' ? '行程总览' : '执行流程'); setDayIndex(0); setSelectedPlace(null); setSelectedNode(null); setPlayback(null);
      const saved = localStorage.getItem(`trip-react-edits-${id}`);
      setEditedPlan(saved ? JSON.parse(saved) : null);
    }
  }
  useEffect(() => {
    if (!task) return;
    const id = task.task_id;
    const source = new EventSource(`/api/trip/tasks/${id}/events?after=${lastSequence.current}`);
    source.onopen = () => setStreamState('实时进度已连接');
    source.onerror = () => setStreamState('正在恢复进度连接');
    const receive = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as Event;
      lastSequence.current = Math.max(lastSequence.current, event.seq);
      setEvents(current => current.some(item => item.seq === event.seq) ? current : [...current, event].sort((a, b) => a.seq - b.seq));
      load(id, false).catch(reason => setError(reason.message));
    };
    ['task.created', 'task.pending', 'task.running', 'task.resumed', 'task.step', 'task.succeeded', 'task.failed', 'task.canceled', 'task.cancel_requested'].forEach(type => source.addEventListener(type, receive as EventListener));
    source.addEventListener('done', () => { source.close(); setStreamState(''); load(id, false).then(refresh).catch(reason => setError(reason.message)); });
    return () => { source.close(); setStreamState(''); };
  }, [task?.task_id]);

  const days = form.start_date && form.end_date ? Math.round((Date.parse(`${form.end_date}T00:00:00+08:00`) - Date.parse(`${form.start_date}T00:00:00+08:00`)) / 86400000) + 1 : 0;
  const plan = editedPlan || task?.plan;
  const day = plan?.days[dayIndex];
  const points = useMemo(() => day?.attractions || [], [day]);
  const busy = !!task && !terminal(task.status);
  const visibleEvents = useMemo(() => playback === null ? events : events.slice(0, playback), [events, playback]);
  const graph = useMemo(() => graphFor(task, visibleEvents, request, playback !== null), [task, visibleEvents, request, playback]);
  const node = selectedNode ? graph.nodes.find(item => item.id === selectedNode.id) : null;
  const cancelRequested = events.some(event => event.type === 'task.cancel_requested');
  function field<K extends keyof Form>(key: K, value: Form[K]) { setForm(current => ({ ...current, [key]: value })); }
  async function submit() {
    setSubmitting(true); setError('');
    const body = JSON.stringify({ ...form, city: form.city.trim(), travel_days: days });
    if (submission.current.body !== body) submission.current = { body, key: crypto.randomUUID() };
    try {
      const result = await api<{ task_id: string }>('/api/trip/plan', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': submission.current.key }, body });
      await load(result.task_id); await refresh();
    } catch (reason) { setError((reason as Error).message); }
    finally { setSubmitting(false); }
  }
  function newTrip() { ++version.current; setTask(null); setEvents([]); setEditedPlan(null); setPlayback(null); setSelectedNode(null); setSelectedPlace(null); setTab('行程总览'); submission.current = { body: '', key: '' }; }
  function move(index: number, delta: number) {
    if (!plan || !task) return;
    const updated = structuredClone(plan);
    const places = updated.days[dayIndex].attractions;
    [places[index], places[index + delta]] = [places[index + delta], places[index]];
    setEditedPlan(updated); localStorage.setItem(`trip-react-edits-${task.task_id}`, JSON.stringify(updated));
  }
  function download() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ ...task, plan, user_adjusted: !!editedPlan }, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = `${task?.city}-${task?.task_id}.json`; link.click(); URL.revokeObjectURL(url);
  }

  return <div className="trip-app">
    <aside className="sidebar"><a className="brand" href="/"><span><Compass size={25}/></span><div>行迹<small>TRAVEL WITH EVIDENCE</small></div></a><div className="sidebar-heading"><h2>规划一段旅行</h2><p>填写条件，交给 Agent 完成信息查询与行程编排。</p></div><form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <label className="field">目的地城市<div className="input-icon"><MapPin size={15}/><input required aria-label="目的地城市" value={form.city} onChange={event => field('city', event.target.value)}/></div></label><div className="date-fields"><label className="field">出发日期<input aria-label="出发日期" type="date" required value={form.start_date} onChange={event => field('start_date', event.target.value)}/></label><label className="field">结束日期<input aria-label="结束日期" type="date" required min={form.start_date} value={form.end_date} onChange={event => field('end_date', event.target.value)}/></label></div><div className="travel-length"><CalendarDays size={13}/>共 {Number.isFinite(days) && days > 0 ? days : '—'} 天</div><label className="field">交通方式<select aria-label="交通方式" value={form.transportation} onChange={event => field('transportation', event.target.value)}>{['公共交通', '自驾', '步行', '打车'].map(value => <option key={value}>{value}</option>)}</select></label><label className="field">住宿偏好<select aria-label="住宿偏好" value={form.accommodation} onChange={event => field('accommodation', event.target.value)}>{['经济型酒店', '舒适型酒店', '高档酒店', '民宿'].map(value => <option key={value}>{value}</option>)}</select></label><label className="field">旅行偏好</label><div className="preferences">{['历史文化', '美食', '自然风光', '博物馆', '亲子', '轻松游'].map(value => <button type="button" key={value} className={form.preferences.includes(value) ? 'selected' : ''} onClick={() => field('preferences', form.preferences.includes(value) ? form.preferences.filter(item => item !== value) : [...form.preferences, value])}>{form.preferences.includes(value) && <Check size={11}/>}{value}</button>)}</div><details className="hotel-options"><summary>酒店品牌与位置</summary><label className="field">酒店品牌<input aria-label="酒店品牌" value={form.hotel_brand} onChange={event => field('hotel_brand', event.target.value)} placeholder="可选，例如华住系"/></label><label className="field">住宿位置<select aria-label="住宿位置" value={form.hotel_location} onChange={event => field('hotel_location', event.target.value)}>{['景点附近', '市中心', '大学附近', '地铁沿线', '火车站附近', '机场附近'].map(value => <option key={value}>{value}</option>)}</select></label><label className="field">具体位置<input aria-label="具体住宿位置" value={form.hotel_location_detail} onChange={event => field('hotel_location_detail', event.target.value)} placeholder="可选，填写地标或地铁站"/></label></details><label className="field">补充要求<textarea aria-label="补充要求" value={form.free_text_input} onChange={event => field('free_text_input', event.target.value)} placeholder="例如：每天安排适量景点，午后留出休息时间。"/></label><button className="primary" disabled={submitting || !form.city.trim() || !Number.isFinite(days) || days < 1 || days > 30} type="submit">{submitting ? <LoaderCircle className="spin" size={16}/> : <Send size={16}/>}生成旅行规划<ArrowRight size={15}/></button>
    </form><div className="sidebar-foot"><span className={`signal ${health ? '' : 'offline'}`}/><span>{health ? '服务已就绪' : '正在检查服务'}<small>{health?.execution_mode || '异步任务'} · 数据来自高德</small></span></div></aside>
    <main className="workspace"><header className="topbar"><span>旅行工作空间<ChevronRight size={13}/><strong>{task?.city || '新建规划'}</strong></span><button onClick={newTrip}><Plus size={15}/>新建规划</button></header><section className="heading"><div><div className="eyebrow">YOUR JOURNEY, CONNECTED</div><h1>{task ? `${task.city}旅行规划` : '智能旅行规划工作台'}</h1><p>{task ? `${task.start_date} 至 ${task.end_date} · ${task.travel_days} 天 · 实际查询数据与执行记录` : '查看地点来源，追踪规划过程，调整每日行程。'}</p></div>{task?.plan && <button className="secondary" onClick={download}><ArrowDownToLine size={15}/>下载完整行程</button>}</section>
      {error && <div className="alert" role="alert"><CircleAlert size={17}/><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError('')}><X size={16}/></button></div>}
      <section className="metrics"><div><CalendarDays size={19}/><span>旅行天数<strong>{task?.travel_days || '—'}<small>天</small></strong></span></div><div><MapPin size={19}/><span>已记录景点<strong>{plan ? plan.days.reduce((sum, item) => sum + item.attractions.length, 0) : '—'}<small>处</small></strong></span></div><div><Clock3 size={19}/><span>规划耗时<strong>{task?.elapsed_ms ? (task.elapsed_ms / 1000).toFixed(1) : '—'}<small>秒</small></strong></span></div><div><Workflow size={19}/><span>任务状态<strong className="metric-state">{task ? stateNames[task.status] : '等待规划'}</strong></span></div></section>
      {task?.warnings.length ? <div className="warning"><CircleAlert size={16}/><div><strong>数据提示</strong>{task.warnings.map((warning, index) => <p key={index}>{warning}</p>)}</div></div> : null}
      {task?.error && <div className="alert"><CircleAlert size={17}/><span>{task.error.message || task.error.code}</span></div>}
      <section className="tabs"><div>{['行程总览', '执行流程', '事件记录', '历史任务'].map(value => <button className={tab === value ? 'active' : ''} key={value} onClick={() => { setTab(value); if (value === '历史任务') refresh().catch(reason => setError(reason.message)); }}>{value}</button>)}</div>{task && <span className={`status ${task.status}`}>{stateNames[task.status]}</span>}</section>
      {busy && <div className="progress-banner"><LoaderCircle size={15} className="spin"/><span>{cancelRequested ? '取消请求已提交，等待当前步骤结束。' : streamState || '任务已提交，正在执行。'}</span><button disabled={cancelRequested} onClick={() => api<Task>(`/api/trip/tasks/${task!.task_id}/cancel`, { method: 'POST' }).then(value => { setTask(value); return load(value.task_id, false); }).catch(reason => setError(reason.message))}><Square size={12}/>取消任务</button></div>}
      {tab === '行程总览' && (plan && day ? <><div className="day-selector">{plan.days.map((item, index) => <button className={dayIndex === index ? 'active' : ''} key={index} onClick={() => { setDayIndex(index); setSelectedPlace(null); }}><span>DAY {String(index + 1).padStart(2, '0')}</span><strong>{item.date}</strong></button>)}{editedPlan && <span className="edited-note">已调整景点顺序，修改保存在当前浏览器。</span>}</div><div className="itinerary-grid"><div><section className="map-card"><div className="section-title"><h2><MapPin size={17}/>景点地图</h2><span>点击标记查看地点信息</span></div><TripMap points={points} mapKey={config.map_key} securityCode={config.map_security_code} onSelect={setSelectedPlace}/></section>{selectedPlace && <section className="place-detail"><div><h3>{selectedPlace.name}</h3><button aria-label="关闭地点详情" onClick={() => setSelectedPlace(null)}><X size={15}/></button></div><p>{selectedPlace.address}</p><p>{selectedPlace.description}</p><small>地点来源：{selectedPlace.source === 'amap' ? '高德已核验' : selectedPlace.source || '未记录'} · POI ID：{selectedPlace.poi_id || '未记录'}</small></section>}<section className="suggestions"><h2><Compass size={17}/>旅行建议</h2><p>{plan.overall_suggestions}</p></section><section className="budget"><h2><Wallet size={17}/>预算估计</h2>{plan.budget ? <><strong>¥ {plan.budget.total.toLocaleString()}</strong><div>{[['景点门票', plan.budget.total_attractions], ['酒店住宿', plan.budget.total_hotels], ['餐饮', plan.budget.total_meals], ['交通', plan.budget.total_transportation]].map(([label, amount]) => <span key={label as string}>{label}<b>¥ {amount}</b></span>)}</div></> : <p>该任务未记录预算。</p>}</section></div><aside className="day-plan"><div className="section-title"><h2>DAY {String(dayIndex + 1).padStart(2, '0')} · 每日行程</h2></div><p className="day-description">{day.description}</p><div className="travel-tags"><span><TrainFront size={13}/>{day.transportation}</span><span><Hotel size={13}/>{day.accommodation}</span></div>{points.map((place, index) => <div className="place-card" key={`${place.poi_id || place.name}-${index}`}><div className="place-top"><span className="place-number">{index + 1}</span><button onClick={() => setSelectedPlace(place)}><strong>{place.name}</strong><small>{place.address}</small></button><div className="reorder"><button aria-label={`上移 ${place.name}`} disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp size={12}/></button><button aria-label={`下移 ${place.name}`} disabled={index === points.length - 1} onClick={() => move(index, 1)}><ArrowDown size={12}/></button></div></div><p>{place.description}</p><div className="place-meta">{place.visit_duration ? <span><Clock3 size={11}/>{place.visit_duration} 分钟</span> : null}<span>{place.source === 'amap' ? '高德已核验' : '来源未记录'}</span>{place.rating ? <span>评分 {place.rating}</span> : null}</div></div>)}{day.hotel && <div className="hotel-card"><Hotel size={19}/><div><h3>{day.hotel.name}</h3><p>{day.hotel.address}</p><small>{day.hotel.price_range}</small></div></div>}{day.meals?.length > 0 && <div className="meal-card"><h3>餐饮安排</h3>{day.meals.map((meal, index) => <p key={index}><strong>{meal.name}</strong> · {meal.description}</p>)}</div>}{plan.weather_info.filter(item => item.date === day.date).map(item => <div className="weather-card" key={item.date}><Sun size={23}/><div><strong>{item.day_weather} · {item.night_temp}–{item.day_temp}°C</strong><p>{item.wind_direction} {item.wind_power} 级</p></div></div>)}</aside></div></> : <div className="empty overview-empty"><span><Compass size={50} strokeWidth={1.1}/></span><h2>{busy ? '正在查询旅行所需信息' : '从目的地开始规划'}</h2><p>{busy ? '执行流程会展示景点、天气、酒店查询和行程编排的实际结果。' : '填写左侧条件，生成包含地点来源、每日安排和地图的旅行计划。'}</p><button onClick={() => setTab(task ? '执行流程' : '历史任务')}>{task ? '查看执行过程' : '查看历史规划'}<ArrowRight size={14}/></button></div>)}
      {tab === '执行流程' && <div className="flow-grid"><section className="flow-panel"><Graph nodes={graph.nodes} edges={graph.edges} onSelect={value => { setSelectedNode(value); setNodeTab('执行详情'); }}/>{task && terminal(task.status) && events.length > 0 && <div className="playback"><span>事件回放</span><input aria-label="事件回放进度" type="range" min={0} max={events.length} value={playback ?? events.length} onChange={event => setPlayback(Number(event.target.value))}/><small>{playback ?? events.length} / {events.length}</small><button onClick={() => setPlayback(null)}>全部事件</button></div>}</section><aside className="node-panel"><div className="section-title"><h2><Layers3 size={16}/>节点详情</h2></div>{node ? <><h3>{node.title}</h3><span className={`status ${node.status}`}>{stateNames[node.status] || node.status}</span><div className="node-tabs">{['执行详情', '输入参数'].map(value => <button key={value} className={nodeTab === value ? 'active' : ''} onClick={() => setNodeTab(value)}>{value}</button>)}</div>{nodeTab === '输入参数' ? <pre>{JSON.stringify(node.detail.arguments || request, null, 2)}</pre> : <><dl>{[['调用工具', node.detail.tool], ['返回条数', node.detail.item_count], ['执行耗时', node.detail.latency_ms !== undefined ? `${node.detail.latency_ms} 毫秒` : undefined], ['调用方式', node.detail.mode]].filter(([, value]) => value !== undefined).map(([label, value]) => <div key={label as string}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><p>{node.detail.detail || node.detail.description || node.detail.error?.message || '实际执行信息保存在任务记录中。'}</p></>}</> : <div className="empty"><Workflow size={32}/><p>点击节点，查看工具参数、返回条数与实际耗时。</p></div>}</aside></div>}
      {tab === '事件记录' && <section className="event-list">{events.length ? visibleEvents.map(event => <details key={event.seq}><summary><span>{String(event.seq).padStart(2, '0')}</span><strong>{eventNames[event.type] || event.type}</strong><small>{event.payload.step || ''}</small></summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details>) : <div className="empty"><Workflow size={32}/><p>提交任务后显示真实执行事件。</p></div>}</section>}
      {tab === '历史任务' && <section className="history-list">{tasks.length ? tasks.map(item => <button key={item.task_id} onClick={() => load(item.task_id).catch(reason => setError(reason.message))}><span className="history-icon"><Navigation size={20}/></span><div><strong>{item.city} · {item.travel_days} 天</strong><p>{item.start_date} 至 {item.end_date}</p></div><span>{item.steps.length} 个执行步骤</span><span className={`status ${item.status}`}>{stateNames[item.status]}</span><ChevronRight size={16}/></button>) : <div className="empty"><History size={32}/><p>生成的规划会保存在这里，刷新页面后仍可查看。</p></div>}</section>}
      <footer className="page-footer"><span>地点与坐标来自高德 · 模型负责行程编排</span><span>React · FastAPI · SSE</span></footer>
    </main>
  </div>;
}
