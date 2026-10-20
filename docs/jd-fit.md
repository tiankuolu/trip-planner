# JD 契合度矩阵（trip-planner-main）

> 目标：让这个项目在**与 JD 真实重合**的部分有可验证的证据，不为凑数加功能。
> 这份文件的规则：**没有 ⬜ 就算完成**。每一行的"证据"列必须是一条能执行的命令或一份能翻出来的输出，
> 不接受"我做了"这种说法。做不到的条目写进 ⛔ 并说明原因，不许假装覆盖。

- 岗位：科研 Agent 全栈开发工程师 / 实习生（玻尔科学空间）
- 项目：`C:\Users\28400\Desktop\trip-planner-main`
- 阶段：P0 已完成（Agent 工具链从 MCP 改回 REST + 原生 function calling + 坐标回填，20-25 秒/次，见文末"已有证据"）

## 进度总览

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 基线（git、venv、本文档） | ✅ |
| M1 | 任务化 + 状态机 + 幂等 + 取消 | ⬜ 数据层已就绪（21 passed） |
| M2 | 队列 + worker + 缓存 + 故障恢复 | ⬜ |
| M3 | 可观测 + 测试补齐 | ⬜ |
| M4 | CI + 容器 + 文档收尾 | ⬜ |
| M5 | 前端真进度与任务视图 | ⬜ |

## 设计依据（对照 AI-Agents-in-Depth）

| 这里的决定 | 书里的依据 |
|---|---|
| 三个工具，不追求工具数量 | 4.2 工具设计的通用原则、4.4 工具太多怎么办 |
| 四步流水线（不共享上下文的多 Agent 协作） | 10.4 不共享上下文的多 Agent 协作、10.5 失败模式 |
| 任务/事件流而非长轮询 | 6.2 异步与事件驱动 |
| `/metrics` + 结构化日志 | 7.8 Agent 的可观测性 |
| 未做 RAG 的理由 | 3.1 用户记忆系统（无历史数据时向量检索无意义） |


---

## M0 基线

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| 工程化规范 | 仓库可用 git 回溯；`backend/venv` 独立环境 | `git log --oneline` 有基线提交；`backend/venv/pyvenv.cfg` 存在 | ✅ |

## M1 任务化 + 状态机

对应：职责 1「任务调度模块的编码实现」、职责 2「状态维护」、要求 5「API 设计逻辑」

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| 任务调度 | `POST /api/trip/plan` → **202** + `{task_id, status_url}`；`GET /api/trip/tasks/{id}` 查状态与结果 | `pytest tests/test_tasks.py::test_submit_returns_202` | ⬜ |
| 状态维护 | 状态机 `pending→running→succeeded/failed/canceled`，非法跃迁抛错；`task_steps` 记录每步耗时/工具/条数 | `pytest tests/test_tasks.py -k state_machine` | ⬜ |
| 数据库 | SQLAlchemy 2.0 模型 + Alembic 迁移；`DATABASE_URL` 默认 SQLite（本机验证），MySQL 可变（标未验证） | `alembic upgrade head` + `pytest tests/test_repo.py -q` | ✅ |
| 幂等 | `Idempotency-Key` 头 + `UNIQUE(idempotency_key)`，重复提交只产生 1 个任务 | `pytest tests/test_tasks.py::test_duplicate_submit_creates_one_task` | ⬜ |
| 取消 | 协作式取消：running 任务在下个检查点退出，状态置 `canceled` | `pytest tests/test_tasks.py::test_cancel_running_task` | ⬜ |
| 进度推送 | `GET /api/trip/tasks/{id}/events`，SSE + `Last-Event-ID` 断线续传 | `curl -N .../events` 抓到 ≥4 条事件；`pytest tests/test_sse.py` | ⬜ |

## M2 队列 + worker + 缓存 + 故障恢复

对应：职责 2「计算任务调度」、要求 5「消息队列、缓存技术」

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| 队列 | `Queue` 抽象 + **SQLite 后端**（租约/可见性超时、指数退避重试、死信表）；`RedisQueue` 另一份实现 | `pytest tests/test_queue.py -q`（含可见性超时、重试、死信） | ⬜ |
| worker | `python -m app.worker` 独立进程消费队列 | 起 worker 后提交任务，`GET /tasks/{id}` 从 pending 变 succeeded | ⬜ |
| 故障恢复 | **杀 worker** 后未完成任务超时重入队并最终完成 | `scripts/fault_kill_worker.ps1` 输出：任务最终 succeeded，且只产生 1 份结果 | ⬜ |
| 缓存 | 高德 POI/天气结果缓存（TTL + 命中率统计） | `/metrics` 里 `cache_hit_ratio`，重复同城请求命中率 > 0 | ⬜ |
| Redis 交付 | `RedisQueue` + `docker-compose.yml` | 代码存在；**本机无 Docker，标注未验证** | ⬜ |

## M3 可观测 + 测试

对应：要求 1「问题排查能力」、职责 5「测试与优化」

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| 日志 | 结构化日志，`request_id / task_id / step` 贯穿；替换现有 137 处 `print` | `grep -c "print(" backend/app` 降到个位数；一条日志能串起整次 run | ⬜ |
| 指标 | `/metrics`：队列深度、任务 P50/P95、失败率、每步耗时、token 用量 | `curl /metrics` 有上述指标且非零 | ⬜ |
| 测试 | pytest 覆盖路由/状态机/队列/幂等/SSE/回填匹配 | `pytest -q` 全绿，用例数 ≥ 30 | ⬜ |

## M4 CI + 容器 + 文档

对应：职责 5「工程化规范并编写基础文档」、要求 5「微服务、CI/CD」

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| CI | GitHub Actions：`ruff` + `pytest` + `vue-tsc` + `vite build` | 本地跑同样命令通过；workflow 文件存在 | ⬜ |
| 容器 | `backend/Dockerfile` + `docker-compose.yml`（api/worker/redis） | 文件存在；**本机无 Docker，标注未验证** | ⬜ |
| 文档 | README 架构图 + 运行手册 + 本矩阵 | README 含 mermaid 架构图与故障注入说明 | ⬜ |

## M5 前端

对应：职责 4「交互界面」、要求 3「Vue」

| JD 条目 | 交付物 | 验收 | 状态 |
|---|---|---|---|
| 真进度 | 提交后跳任务页，SSE 推进度（替换现在的假进度条） | 浏览器实测：进度条随真实步骤变化 | ⬜ |
| 任务视图 | 任务列表 + 失败原因 + 步骤时间线 | dev server 页面可访问、数据来自接口 | ⬜ |

---

## ⛔ 明确不覆盖（写清原因，不假装）

| JD 条目 | 为什么不做 |
|---|---|
| React（职责 4、要求 3） | 现有 Vue 3 实现完整可用。仅换语法不增能力，属于"为加而加"。Vue 已在要求 3 的"React/Vue 等至少一种"范围内。 |
| 科研领域认知 / 计算任务调度（加分 1、4） | 旅行域没有科研算例。硬塞一个假科研流程会立刻被追问穿。这条靠你本人的轨道动力学背景回答，不靠代码。 |
| RAG / 向量检索（要求 4、加分 2） | 当前没有真实语料。等 M1/M2 持久化跑起来、积累十几条真实 run 之后，偏好/经验才有检索价值；现在做只能是假数据演示。 |
| Google ADK / LangChain 迁移（要求 4） | 迁移框架不产生新能力，现有原生 function calling 已能讲清工具集成。多智能体这点现有四步编排可作证据。 |
| MySQL 实际部署 | 无 Docker/WSL（WSL2 报 `NO_SYSTEM_RESOURCES`）。交付迁移脚本与 `DATABASE_URL` 切换，标注未验证。 |
| 知识图谱 / 多模态 / 语音 / K8s | 与岗位相关性低，两周内做不完，做完也讲不清。 |
| GitHub 开源 PR（加分 3） | 需要账号与长期维护，本 goal 内不承诺。提交历史只能算弱证据。 |

---

## 已有证据（P0 阶段，已完成）

| JD 条目 | 证据 |
|---|---|
| 智能体集成层 | 3 个工具 `amap_search_poi / amap_search_hotel / amap_get_weather`，`mode=function_call`，见 `/api/trip/health` |
| 多智能体协同编排 | 四步流水线（景点→天气→酒店→规划），`trace` 里每步工具/参数/条数/耗时 |
| 接口实现 | `/api/trip/plan`、`/api/map/*`、`/api/poi/*`，失败返回 502 带原因 |
| 数据可信 | 9/9 景点坐标为高德回填，`source=amap`，含真实 POI ID |
| 测试 | `pytest tests -q` → 11 passed（名称匹配/距离/字段容错） |
| 前端 | `npm run build` 通过（vue-tsc + vite） |
| 问题排查 | 定位并修复：MCP 工具 0 发现、`asyncio.run` 撞事件循环、文本协议 vs 原生 function calling、静默降级伪成功 |

## 运行时约束（决定验收边界）

| 约束 | 现状 | 影响 |
|---|---|---|
| Docker | 未安装 | 容器与 Redis 只能代码交付 |
| WSL2 | 启动失败 `ERROR_NO_SYSTEM_RESOURCES` | 不能用 Linux 侧服务 |
| Redis | 未安装 | 队列本机验证走 SQLite 后端 |
| Python | 3.11.9（py launcher） | 新建 `backend/venv`，不再往全局装 |
| LLM | DeepSeek 兼容端点，`LLM_THINKING=disabled` | 强制 `tool_choice` 可用 |
| 单次真实生成 | 20-25 秒，成本 <0.1 元 | 可作为验收手段，单轮上限 20 次 |

## 执行红线

- 我可以：改代码、加依赖（装进 `backend/venv`）、跑测试与真实生成、本地 commit（**不 push，无远端**）、改本文档。
- 我必须先问：改/删 `.env` 里的 key、删除已有文件、回退已提交内容、连续 3 轮拿不出证据、单轮真实调用超 20 次。
