"""旅行规划 API：提交任务 + 自检

`POST /api/trip/plan` 已改为**异步任务语义**（202 + task_id）：
一次生成要 20 秒，同步接口会让浏览器和线程池都在等，而且没法重试、取消、恢复。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from ...agents.trip_planner_agent import get_trip_planner_agent
from ...db import session_scope
from ...models.schemas import ErrorResponse, TaskSubmitResponse, TripRequest
from ...repositories import task_store as store
from ...services.task_runner import get_task_runner

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/plan",
    status_code=202,
    response_model=TaskSubmitResponse,
    summary="提交旅行规划任务（异步）",
    description=(
        "提交后立刻返回 202 + task_id，用 `/api/trip/tasks/{task_id}` 查状态，"
        "或订阅 `/api/trip/tasks/{task_id}/events` 拿实时进度。"
        "带相同的 `Idempotency-Key` 重复提交只会产生一个任务。"
    ),
    responses={422: {"model": ErrorResponse, "description": "参数不合法"}},
)
def plan_trip(
    request: TripRequest,
    background: BackgroundTasks,
    idempotency_key: Optional[str] = Header(
        None, alias="Idempotency-Key", description="幂等键，建议用前端生成的 UUID"
    ),
    x_request_id: Optional[str] = Header(None, alias="X-Request-Id"),
):
    with session_scope() as session:
        task, created = store.create_task(
            session,
            request,
            request.model_dump(),
            idempotency_key=idempotency_key,
            request_id=x_request_id,
        )
        task_id = task.id
        status = task.status

    if created:
        background.add_task(_run_safely, task_id)

    base = f"/api/trip/tasks/{task_id}"
    return TaskSubmitResponse(
        task_id=task_id,
        status=status,
        status_url=base,
        events_url=f"{base}/events",
        deduplicated=not created,
    )


def _run_safely(task_id: str) -> None:
    """后台执行：异常必须吞掉，否则会在响应已返回后炸在后台"""
    try:
        get_task_runner().execute(task_id)
    except Exception as exc:  # noqa: BLE001 - 已落库的失败由 runner 负责，这里只兜底
        print(f"❌ 任务执行异常 {task_id}: {exc}")


@router.get("/health", summary="健康检查", description="检查模型配置、高德 Key、可用工具与数据库")
def health_check():
    try:
        info = get_trip_planner_agent().health()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"服务不可用: {exc}") from exc

    from ...db import get_database_url

    try:
        with session_scope() as session:
            pending = len(store.list_tasks(session, store.PENDING, limit=100))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"数据库不可用: {exc}") from exc

    database = "sqlite" if get_database_url().startswith("sqlite") else "mysql/other"
    return {
        "status": "healthy",
        "service": "trip-planner",
        **info,
        "database": database,
        "pending_tasks": pending,
    }
