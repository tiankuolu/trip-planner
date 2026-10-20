"""高德地图服务封装（原生 REST 直连）

设计说明
--------
早期版本通过 MCP(uvx amap-mcp-server) 调用高德，实际上有三处硬伤：

1. 依赖本机存在 `uvx`，否则 MCP 服务器起不来；
2. hello_agents 的 MCPTool 内部用 `asyncio.run()` 包异步客户端，在 FastAPI
   的 async 路由里调用会命中 "asyncio.run() cannot be called from a running
   event loop"，而库把异常吞成字符串返回，错误被静默；
3. 工具发现失败时 `_available_tools = []`，Agent 拿到 0 个工具后仍照常运行，
   最终输出的是模型编造的景点和天气。

因此这里改成直接调用高德 Web 服务 REST 接口：同步、可测、错误显式抛出。
所有返回的都是高德真实数据，不含任何模型生成内容。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings
from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo

AMAP_BASE = "https://restapi.amap.com/v3"
DEFAULT_TIMEOUT = 12.0


class AmapError(RuntimeError):
    """高德接口调用失败（配置缺失 / 网络异常 / 业务错误码）"""


def _txt(value: Any) -> str:
    """高德在字段为空时经常返回 []，统一转成字符串"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "" if not value else _txt(value[0])
    if isinstance(value, dict):
        return _txt(value.get("value", ""))
    return str(value).strip()


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(_txt(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(_txt(value)))
    except (TypeError, ValueError):
        return default


def _parse_location(raw: Any) -> Optional[Location]:
    """高德坐标格式为 "lng,lat" """
    text = _txt(raw)
    if "," not in text:
        return None
    lng, lat = text.split(",", 1)
    try:
        return Location(longitude=float(lng), latitude=float(lat))
    except ValueError:
        return None


class AmapService:
    """高德地图服务封装（同步客户端，可直接在线程池中调用）"""

    def __init__(self) -> None:
        self.settings = get_settings()
        if not self.settings.amap_api_key:
            raise AmapError("AMAP_API_KEY 未配置，请在 backend/.env 中设置")
        self.key = self.settings.amap_api_key
        self._adcode_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ 基础
    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        clean["key"] = self.key
        url = f"{AMAP_BASE}{path}"
        try:
            resp = httpx.get(url, params=clean, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise AmapError(f"高德接口请求失败 {path}: {exc}") from exc
        except ValueError as exc:
            raise AmapError(f"高德接口返回非 JSON {path}: {exc}") from exc

        if str(data.get("status")) != "1":
            raise AmapError(
                f"高德接口返回错误 {path}: {data.get('info')} (infocode={data.get('infocode')})"
            )
        return data

    # ------------------------------------------------------------------ 地理编码
    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """地址 -> 经纬度"""
        data = self._get("/geocode/geo", {"address": address, "city": city})
        geocodes = data.get("geocodes") or []
        if not geocodes:
            return None
        return _parse_location(geocodes[0].get("location"))

    def get_adcode(self, city: str) -> str:
        """城市名 -> adcode（天气接口需要）"""
        if city in self._adcode_cache:
            return self._adcode_cache[city]
        data = self._get("/geocode/geo", {"address": city})
        geocodes = data.get("geocodes") or []
        adcode = _txt(geocodes[0].get("adcode")) if geocodes else ""
        if not adcode:
            raise AmapError(f"无法解析城市 adcode: {city}")
        self._adcode_cache[city] = adcode
        return adcode

    # ------------------------------------------------------------------ POI
    def search_poi_raw(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
        types: Optional[str] = None,
        offset: int = 10,
        page: int = 1,
    ) -> List[Dict[str, Any]]:
        """搜索 POI，返回高德原始字段整理后的字典列表（供工具层使用）"""
        data = self._get(
            "/place/text",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": "true" if citylimit else "false",
                "types": types,
                "offset": offset,
                "page": page,
                "extensions": "all",
            },
        )
        results: List[Dict[str, Any]] = []
        for poi in data.get("pois") or []:
            location = _parse_location(poi.get("location"))
            if location is None:
                continue
            biz = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
            photos = [
                _txt(p.get("url"))
                for p in (poi.get("photos") or [])
                if isinstance(p, dict) and _txt(p.get("url"))
            ]
            results.append(
                {
                    "id": _txt(poi.get("id")),
                    "name": _txt(poi.get("name")),
                    "type": _txt(poi.get("type")),
                    "typecode": _txt(poi.get("typecode")),
                    "address": _txt(poi.get("address")),
                    "cityname": _txt(poi.get("cityname")),
                    "adname": _txt(poi.get("adname")),
                    "tel": _txt(poi.get("tel")),
                    "location": location,
                    "rating": _to_float(biz.get("rating")),
                    "cost": _to_float(biz.get("cost")),
                    "photos": photos,
                }
            )
        return results

    def search_poi(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
        types: Optional[str] = None,
        offset: int = 10,
    ) -> List[POIInfo]:
        """搜索 POI（对外的 HTTP 接口使用，已做模型适配）"""
        pois = self.search_poi_raw(keywords, city, citylimit, types, offset)
        return [
            POIInfo(
                id=p["id"],
                name=p["name"],
                type=p["type"] or "未知类型",
                address=p["address"] or f"{p['cityname']}{p['adname']}",
                location=p["location"],
                tel=p["tel"] or None,
            )
            for p in pois
            if p["name"]
        ]

    # ------------------------------------------------------------------ 天气
    def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询城市天气（高德预报接口，最多返回未来 4 天）"""
        adcode = self.get_adcode(city)
        data = self._get("/weather/weatherInfo", {"city": adcode, "extensions": "all"})
        forecasts = data.get("forecasts") or []
        if not forecasts:
            raise AmapError(f"高德未返回 {city} 的天气数据")
        casts = forecasts[0].get("casts") or []
        return [
            WeatherInfo(
                date=_txt(c.get("date")),
                day_weather=_txt(c.get("dayweather")),
                night_weather=_txt(c.get("nightweather")),
                day_temp=_txt(c.get("daytemp")),
                night_temp=_txt(c.get("nighttemp")),
                wind_direction=_txt(c.get("daywind")),
                wind_power=_txt(c.get("daypower")),
            )
            for c in casts
        ]

    def get_weather_raw(self, city: str) -> Dict[str, Any]:
        """天气字典（给工具层喂给模型，字段名保持语义清晰）"""
        weather = self.get_weather(city)
        return {"city": city, "count": len(weather), "forecasts": [w.model_dump() for w in weather]}

    # ------------------------------------------------------------------ 路线
    def plan_route_by_coords(
        self,
        origin_lng: float,
        origin_lat: float,
        dest_lng: float,
        dest_lat: float,
        city: str = "",
        route_type: str = "transit",
    ) -> Optional[RouteInfo]:
        """按坐标规划路线，返回 None 表示未找到可行路线"""
        origin = f"{origin_lng},{origin_lat}"
        destination = f"{dest_lng},{dest_lat}"

        if route_type == "walking":
            path = "/direction/walking"
        elif route_type == "driving":
            path = "/direction/driving"
        else:
            path = "/direction/transit/integrated"

        params: Dict[str, Any] = {"origin": origin, "destination": destination}
        if route_type == "transit" and city:
            params["city"] = city

        data = self._get(path, params)

        if route_type == "transit":
            transits = (data.get("route") or {}).get("transits") or []
            if not transits:
                return None
            first = transits[0]
            lines: List[str] = []
            for seg in first.get("segments") or []:
                for busline in (seg.get("bus") or {}).get("buslines") or []:
                    name = _txt(busline.get("name"))
                    if name:
                        lines.append(name)
            return RouteInfo(
                distance=_to_int(first.get("distance")),
                duration=_to_int(first.get("duration")),
                route_type="transit",
                description=" + ".join(lines[:3]) or "公交出行",
            )

        paths = (data.get("route") or {}).get("paths") or []
        if not paths:
            return None
        first_path = paths[0]
        steps = first_path.get("steps") or []
        instruction = _txt(steps[0].get("instruction")) if steps else ""
        return RouteInfo(
            distance=_to_int(first_path.get("distance")),
            duration=_to_int(first_path.get("duration")),
            route_type=route_type,
            description=instruction[:40] or ("步行" if route_type == "walking" else "驾车"),
        )

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Optional[RouteInfo]:
        """按地址规划路线（先地理编码，再按坐标规划）"""
        city = origin_city or destination_city or ""
        origin = self.geocode(origin_address, origin_city)
        dest = self.geocode(destination_address, destination_city or origin_city)
        if origin is None or dest is None:
            raise AmapError("起点或终点地址无法解析为坐标")
        return self.plan_route_by_coords(
            origin.longitude,
            origin.latitude,
            dest.longitude,
            dest.latitude,
            city=city,
            route_type=route_type,
        )

    # ------------------------------------------------------------------ 图片
    def search_poi_photos(self, name: str, city: str, limit: int = 5) -> List[str]:
        """景点图片（走高德 POI 搜索的 photos 字段）"""
        try:
            pois = self.search_poi_raw(name, city, offset=1)
        except AmapError:
            return []
        if not pois:
            return []
        return pois[0]["photos"][:limit]

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """POI 详情（高德 /v3/place/detail），含图片、营业时间、评分等"""
        data = self._get("/place/detail", {"id": poi_id, "extensions": "all"})
        pois = data.get("pois") or []
        if not pois:
            return {}
        poi = pois[0]
        biz = poi.get("biz_ext") if isinstance(poi.get("biz_ext"), dict) else {}
        return {
            "id": _txt(poi.get("id")),
            "name": _txt(poi.get("name")),
            "type": _txt(poi.get("type")),
            "address": _txt(poi.get("address")),
            "location": _parse_location(poi.get("location")).model_dump()
            if _parse_location(poi.get("location"))
            else None,
            "tel": _txt(poi.get("tel")),
            "rating": _to_float(biz.get("rating")),
            "cost": _to_float(biz.get("cost")),
            "open_time": _txt(biz.get("open_time")),
            "photos": [
                _txt(p.get("url"))
                for p in (poi.get("photos") or [])
                if isinstance(p, dict) and _txt(p.get("url"))
            ],
        }


_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    """获取高德服务实例（单例，内部无状态，可跨线程复用）"""
    global _service
    if _service is None:
        _service = AmapService()
    return _service


def reset_amap_service() -> None:
    global _service
    _service = None
