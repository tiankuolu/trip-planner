"""地图服务 API 路由

全部走同步 `def`：内部是高德 REST 调用，FastAPI 会放进线程池，
不会阻塞事件循环。上游失败直接抛 502，不再返回 "success: true + 空数组"。
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ...models.schemas import (
    POISearchResponse,
    RouteInfo,
    RouteRequest,
    RouteResponse,
    WeatherResponse,
)
from ...services.amap_service import AmapError, get_amap_service

router = APIRouter(prefix="/map", tags=["地图服务"])


def _service():
    try:
        return get_amap_service()
    except AmapError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/poi",
    response_model=POISearchResponse,
    summary="搜索POI",
    description="根据关键词搜索POI（高德 Web 服务 /v3/place/text）",
)
def search_poi(
    keywords: str = Query(..., description="搜索关键词", examples=["故宫"]),
    city: str = Query(..., description="城市", examples=["北京"]),
    citylimit: bool = Query(True, description="是否限制在城市范围内"),
):
    try:
        pois = _service().search_poi(keywords, city, citylimit)
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"POI搜索失败: {exc}") from exc

    return POISearchResponse(
        success=True,
        message=f"POI搜索成功，共 {len(pois)} 条",
        data=pois,
    )


@router.get(
    "/weather",
    response_model=WeatherResponse,
    summary="查询天气",
    description="查询指定城市的天气预报（高德 /v3/weather/weatherInfo，未来最多4天）",
)
def get_weather(city: str = Query(..., description="城市名称", examples=["北京"])):
    try:
        weather = _service().get_weather(city)
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"天气查询失败: {exc}") from exc

    return WeatherResponse(
        success=True,
        message=f"天气查询成功，共 {len(weather)} 天预报",
        data=weather,
    )


@router.post(
    "/route",
    response_model=RouteResponse,
    summary="规划路线（按地址）",
    description="先地理编码再规划路线",
)
def plan_route(request: RouteRequest):
    try:
        route = _service().plan_route(
            origin_address=request.origin_address,
            destination_address=request.destination_address,
            origin_city=request.origin_city,
            destination_city=request.destination_city,
            route_type=request.route_type,
        )
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"路线规划失败: {exc}") from exc

    if route is None:
        return RouteResponse(success=False, message="未找到可行路线", data=None)
    return RouteResponse(success=True, message="路线规划成功", data=route)


@router.get(
    "/route-between",
    response_model=RouteResponse,
    summary="规划两点间路线（按坐标）",
    description="根据经纬度规划两点之间路线，支持步行/驾车/公共交通",
)
def plan_route_between(
    origin_lng: float = Query(..., description="起点经度", examples=[116.397128]),
    origin_lat: float = Query(..., description="起点纬度", examples=[39.916527]),
    destination_lng: float = Query(..., description="终点经度", examples=[116.397428]),
    destination_lat: float = Query(..., description="终点纬度", examples=[39.909527]),
    city: str = Query("", description="城市名，公交规划必填", examples=["北京"]),
    route_type: str = Query("transit", description="路线类型: walking/driving/transit"),
):
    try:
        route: Optional[RouteInfo] = _service().plan_route_by_coords(
            origin_lng,
            origin_lat,
            destination_lng,
            destination_lat,
            city=city,
            route_type=route_type,
        )
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"路线规划失败: {exc}") from exc

    if route is None:
        return RouteResponse(success=False, message="未找到可行路线", data=None)
    return RouteResponse(success=True, message="路线规划成功", data=route)


@router.get(
    "/health",
    summary="健康检查",
    description="检查高德 Key 与上游接口是否可用",
)
def health_check():
    service = _service()
    try:
        weather = service.get_weather("北京")
    except AmapError as exc:
        raise HTTPException(status_code=503, detail=f"高德上游不可用: {exc}") from exc

    return {
        "status": "healthy",
        "service": "map-service",
        "amap_key_configured": bool(service.key),
        "probe_weather_days": len(weather),
    }
