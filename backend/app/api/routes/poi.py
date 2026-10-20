"""POI 相关 API 路由（同步 def，直连高德 REST）"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services.amap_service import AmapError, get_amap_service

router = APIRouter(prefix="/poi", tags=["POI"])


class POIDetailResponse(BaseModel):
    """POI 详情响应"""

    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None


def _service():
    try:
        return get_amap_service()
    except AmapError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/detail/{poi_id}",
    response_model=POIDetailResponse,
    summary="获取POI详情",
    description="根据 POI ID 获取详细信息，包括图片、营业时间、评分",
)
def get_poi_detail(poi_id: str):
    try:
        detail = _service().get_poi_detail(poi_id)
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"获取POI详情失败: {exc}") from exc

    if not detail:
        return POIDetailResponse(success=False, message="未找到该 POI", data=None)
    return POIDetailResponse(success=True, message="获取POI详情成功", data=detail)


@router.get(
    "/search",
    summary="搜索POI",
    description="根据关键词搜索POI（返回高德原始结构，含评分与参考价）",
)
def search_poi(keywords: str, city: str = "北京"):
    try:
        pois = _service().search_poi(keywords, city)
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"搜索POI失败: {exc}") from exc

    return {"success": True, "message": f"搜索成功，共 {len(pois)} 条", "data": pois}


@router.get(
    "/photo",
    summary="获取景点图片",
    description="通过高德 POI 的 photos 字段获取景点真实图片",
)
def get_attraction_photo(name: str, city: str = "北京"):
    try:
        photos: List[str] = _service().search_poi_photos(name, city)
    except AmapError as exc:
        raise HTTPException(status_code=502, detail=f"获取景点图片失败: {exc}") from exc

    return {
        "success": True,
        "message": "获取图片成功" if photos else "未找到图片",
        "data": {
            "name": name,
            "photos": photos,
            "photo_url": photos[0] if photos else None,
        },
    }
