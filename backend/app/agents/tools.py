"""Agent 工具层：工具 schema + 本地执行器

设计原则
--------
模型只负责"决定搜什么"（给出关键词、城市、品牌等参数），
工具调用**永远由本地代码执行**，返回的都是高德真实数据。
这样既保留了 function calling 的灵活性，又不会出现模型自己编数据的情况。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..services.amap_service import AmapError, get_amap_service
from ..services.cache import cached

# ---------------------------------------------------------------- 工具 schema

TOOL_SEARCH_POI: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_search_poi",
        "description": "按关键词搜索指定城市的景点/博物馆/公园等 POI，返回名称、地址、经纬度、类型",
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "string",
                    "description": "搜索关键词，例如：历史文化、博物馆、西湖、亲子场馆",
                },
                "city": {"type": "string", "description": "城市名，例如：杭州"},
            },
            "required": ["keywords", "city"],
        },
    },
}

TOOL_SEARCH_HOTEL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_search_hotel",
        "description": "搜索指定城市的酒店住宿 POI，可限定品牌或某个地标附近，返回名称、地址、经纬度、评分、参考价",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名"},
                "brand": {
                    "type": "string",
                    "description": "酒店品牌或系列，例如：汉庭、全季、如家；不确定时留空字符串",
                },
                "near": {
                    "type": "string",
                    "description": "希望靠近的地标，例如：西湖、市中心；不确定时留空字符串",
                },
            },
            "required": ["city"],
        },
    },
}

TOOL_GET_WEATHER: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "amap_get_weather",
        "description": "查询指定城市未来几天的天气预报（白天/夜间天气、气温、风向风力）",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}

# ---------------------------------------------------------------- 执行器


def _run_search_poi(args: Dict[str, Any]) -> Dict[str, Any]:
    keywords = str(args.get("keywords", "")).strip() or "景点"
    city = str(args.get("city", "")).strip()
    if not city:
        raise AmapError("搜索景点缺少 city 参数")

    def produce() -> Dict[str, Any]:
        pois = get_amap_service().search_poi_raw(keywords, city, offset=10)
        return {
            "keywords": keywords,
            "city": city,
            "count": len(pois),
            "pois": [
                {
                    "name": p["name"],
                    "address": p["address"] or f"{p['cityname']}{p['adname']}",
                    "location": p["location"].model_dump(),
                    "type": p["type"],
                    "rating": p["rating"],
                    "tel": p["tel"],
                    "id": p["id"],
                }
                for p in pois
                if p["name"]
            ],
        }

    return cached("poi", f"poi:{city}:{keywords}", produce)


def _run_search_hotel(args: Dict[str, Any]) -> Dict[str, Any]:
    city = str(args.get("city", "")).strip()
    if not city:
        raise AmapError("搜索酒店缺少 city 参数")
    brand = str(args.get("brand", "") or "").strip()
    near = str(args.get("near", "") or "").strip()

    def produce() -> Dict[str, Any]:
        service = get_amap_service()
        queries = [
            q
            for q in (f"{brand}酒店" if brand else "", f"{near}酒店" if near else "", "酒店")
            if q
        ]

        merged: Dict[str, Dict[str, Any]] = {}
        for query in dict.fromkeys(queries):
            for poi in service.search_poi_raw(query, city, types="100000", offset=8):
                if poi["name"] and poi["id"] not in merged:
                    merged[poi["id"]] = poi
            if len(merged) >= 8:
                break

        hotels = list(merged.values())[:8]
        return {
            "city": city,
            "queries": queries,
            "count": len(hotels),
            "hotels": [
                {
                    "name": p["name"],
                    "address": p["address"] or f"{p['cityname']}{p['adname']}",
                    "location": p["location"].model_dump(),
                    "rating": p["rating"],
                    "cost": p["cost"],
                    "id": p["id"],
                }
                for p in hotels
            ],
        }

    return cached("hotel", f"hotel:{city}:{brand}:{near}", produce)


def _run_get_weather(args: Dict[str, Any]) -> Dict[str, Any]:
    city = str(args.get("city", "")).strip()
    if not city:
        raise AmapError("查询天气缺少 city 参数")
    return cached("weather", f"weather:{city}", lambda: get_amap_service().get_weather_raw(city))


TOOLS: Dict[str, Dict[str, Any]] = {
    "amap_search_poi": {"schema": TOOL_SEARCH_POI, "handler": _run_search_poi},
    "amap_search_hotel": {"schema": TOOL_SEARCH_HOTEL, "handler": _run_search_hotel},
    "amap_get_weather": {"schema": TOOL_GET_WEATHER, "handler": _run_get_weather},
}


def get_tool_schema(name: str) -> Dict[str, Any]:
    if name not in TOOLS:
        raise KeyError(f"未知工具: {name}")
    return TOOLS[name]["schema"]


def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """执行工具，异常向上抛（由编排层决定是降级还是失败）"""
    if name not in TOOLS:
        raise KeyError(f"未知工具: {name}")
    return TOOLS[name]["handler"](arguments or {})


def list_tools() -> List[str]:
    return list(TOOLS)
