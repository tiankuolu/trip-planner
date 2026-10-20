"""旅行规划 API 路由

路由全部用同步 `def` 声明：编排流程内部是阻塞的（HTTP 调用 + LLM 调用），
交给 FastAPI 的线程池执行，既能并行多个请求，也不会阻塞事件循环
——这正是旧版本 "asyncio.run() cannot be called from a running event loop"
的根因。
"""

from fastapi import APIRouter, HTTPException

from ...agents.trip_planner_agent import PlanGenerationError, get_trip_planner_agent
from ...models.schemas import ErrorResponse, TripPlanResponse, TripRequest

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description=(
        "根据用户输入生成旅行计划。景点/天气/酒店均来自高德真实数据，"
        "坐标经过回填校验；任何步骤降级都会在 degraded / warnings 中体现。"
    ),
    responses={502: {"model": ErrorResponse, "description": "模型或上游服务失败"}},
)
def plan_trip(request: TripRequest):
    print(
        f"\n[plan] city={request.city} {request.start_date}~{request.end_date} "
        f"days={request.travel_days} prefs={request.preferences}"
    )

    try:
        result = get_trip_planner_agent().plan_trip(request)
    except PlanGenerationError as exc:
        print(f"❌ 行程生成失败: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sources = result.sources
    print(
        f"[plan] done in {result.elapsed_ms}ms "
        f"attractions={sources.get('attractions')} "
        f"grounded={sources.get('attractions_grounded')} "
        f"weather={sources.get('weather')} "
        f"hotels={sources.get('hotels')} "
        f"warnings={len(result.warnings)}"
    )

    message = "旅行计划生成成功"
    if result.degraded:
        message = f"旅行计划生成成功（{len(result.warnings)} 项数据降级，详见 warnings）"

    return TripPlanResponse(
        success=True,
        message=message,
        data=result.plan,
        degraded=result.degraded,
        warnings=result.warnings,
        trace=result.trace,
        elapsed_ms=result.elapsed_ms,
        data_sources=result.sources,
    )


@router.get(
    "/health",
    summary="健康检查",
    description="检查模型配置、高德 Key 与可用工具",
)
def health_check():
    try:
        agent = get_trip_planner_agent()
        info = agent.health()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"服务不可用: {exc}") from exc

    return {"status": "healthy", "service": "trip-planner", **info}
