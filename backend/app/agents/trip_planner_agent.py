"""多 Agent 行程编排

流水线（每一步都独立可观测、可降级）：

    1. 景点  LLM 抽关键词 ──► amap_search_poi  ──► 高德真实 POI
    2. 天气  LLM 抽城市   ──► amap_get_weather ──► 高德真实预报
    3. 酒店  LLM 抽品牌/地标 ► amap_search_hotel ► 高德真实住宿
    4. 规划  LLM 仅用上面三份真实数据出 JSON ──► pydantic 校验 ──► 坐标回填

与旧实现的三个关键差别：

* 工具调用由本地代码执行，模型只给参数——不会出现"模型自己编景点"；
* 坐标、地址、评分、电话一律以高德返回为准，模型给的名字只用来匹配；
* 任何一步失败都会写进 warnings 并置 degraded，而不是悄悄返回一份假行程。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from ..models.schemas import (
    Attraction,
    Budget,
    DayPlan,
    Location,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..observability import bind_step, log
from ..services.amap_service import AmapError, get_amap_service
from ..services.llm_service import LLMError, get_llm
from .tools import execute_tool, get_tool_schema, list_tools


class PlanGenerationError(RuntimeError):
    """行程生成失败（会以明确的错误返回给前端，不再伪造数据）"""


class TaskCanceled(RuntimeError):
    """任务被用户取消：在检查点抛出，由 runner 落成 canceled 状态"""


# ==================== 提示词 ====================

POI_PARAM_PROMPT = """你是景点搜索的参数抽取器。根据用户要求，给出高德地图 POI 搜索关键词。

规则：
1. 必须调用 amap_search_poi 工具，不要输出解释；
2. keywords 用 2-6 个字的中文名词，优先用用户偏好（如"历史文化"→"博物馆"或"历史建筑"），
   不要带城市名，不要用"景点"这种过泛的词（除非用户没有任何偏好）；
3. city 用用户给的城市名。"""

HOTEL_PARAM_PROMPT = """你是酒店搜索的参数抽取器。根据用户要求，给出高德地图住宿搜索参数。

规则：
1. 必须调用 amap_search_hotel 工具，不要输出解释；
2. city 用用户给的城市名；
3. brand：用户指定了酒店品牌/系列就填（如"汉庭"），否则空字符串；
4. near：用户指定了位置偏好（景点附近/大学附近/市中心/某地标）就填该地标名，否则空字符串。"""

WEATHER_PARAM_PROMPT = """你是天气查询的参数抽取器。

规则：
1. 必须调用 amap_get_weather 工具，不要输出解释；
2. city 用用户给的城市名（去掉"市""省"后缀也可以）。"""

PLANNER_PROMPT = """你是行程规划专家。你会拿到三份**真实数据**：候选景点（含高德坐标）、真实天气预报、候选酒店。
你的唯一任务是把它们编排成一份可执行的行程 JSON。

硬性规则：
1. 景点只能从"候选景点"里选，且 name 必须与候选项**完全一致**（用于回填坐标，写错就对不上）；
2. 酒店只能从"候选酒店"里选，name 同样必须完全一致；同一家酒店尽量连住，避免每天换店；
   候选项里的 distance_km 是到景点群中心的直线距离，优先选距离小的；超过 10 公里要在 description 里说明理由；
3. 每天 2-3 个景点，按地理位置就近分组（参考经纬度），避免跨城折返；
4. 每天必须有早/中/晚三餐（餐饮可以自己推荐，不需要候选数据）；
5. weather_info 只能使用给定的真实预报：预报里有的日期照抄，没有的日期**不要编写**，
   直接不放进 weather_info 数组，并在 overall_suggestions 里说明"该日期超出预报范围"；
6. 不要编造景点、门票、评分、坐标、天气。候选数据里没有的信息可以留空；
7. budget 各分项相加必须等于 total；
8. 只输出一个 JSON 对象，不要 markdown 代码块，不要解释。

输出结构：
{
  "city": "城市",
  "start_date": "YYYY-MM-DD",
  "end_date": "YYYY-MM-DD",
  "days": [{
    "date": "YYYY-MM-DD",
    "day_index": 0,
    "description": "当日概述（说明动线和为什么这样安排）",
    "transportation": "交通方式建议",
    "accommodation": "住宿安排说明",
    "hotel": {"name": "候选酒店名", "price_range": "300-400元", "estimated_cost": 350, "distance": "距景点x公里"},
    "attractions": [{
      "name": "候选景点名",
      "visit_duration": 120,
      "description": "为什么值得去、怎么玩",
      "category": "类别",
      "ticket_price": 0
    }],
    "meals": [
      {"type": "breakfast", "name": "推荐店/菜品", "description": "说明", "estimated_cost": 25},
      {"type": "lunch", "name": "...", "description": "...", "estimated_cost": 60},
      {"type": "dinner", "name": "...", "description": "...", "estimated_cost": 80}
    ]
  }],
  "weather_info": [{"date": "YYYY-MM-DD", "day_weather": "晴", "night_weather": "多云",
                    "day_temp": 25, "night_temp": 15, "wind_direction": "南风", "wind_power": "1-3级"}],
  "overall_suggestions": "整体建议（含预约、穿衣、交通、避坑）",
  "budget": {"total_attractions": 0, "total_hotels": 0, "total_meals": 0,
             "total_transportation": 0, "total": 0}
}
"""


# ==================== 结果容器 ====================


@dataclass
class PlanResult:
    plan: TripPlan
    warnings: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    sources: Dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    token_usage: int = 0

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


# ==================== 名称匹配（用于坐标回填）====================


def _normalize_name(name: str) -> str:
    """规整 POI 名：保留括号里的分店/馆区信息，只去掉括号符号和通用后缀

    关键取舍：括号内容**必须保留**。像"汉庭酒店(杭州文新地铁站店)"和
    "汉庭酒店(杭州西湖仁和路店)"，去掉括号就都变成"汉庭酒店"，
    两个分店会被误判成同一个地方，把 A 店的坐标安到 B 店头上。
    """
    text = str(name or "").strip()
    for char in "（）()【】[]「」":
        text = text.replace(char, "")
    for token in ("景区", "风景区", "旅游区"):
        text = text.replace(token, "")
    return text.replace(" ", "").replace("·", "").lower()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    return len(shared) / len(set(a) | set(b))


def _distance_km(origin: Dict[str, float], target: Dict[str, float]) -> float:
    """两点球面距离（公里），用于判断酒店离景点群有多远"""
    lat1, lng1 = origin["latitude"], origin["longitude"]
    lat2, lng2 = target["latitude"], target["longitude"]
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _best_match(
    name: str, candidates: List[Dict[str, Any]], min_similarity: float = 0.6
) -> Optional[Dict[str, Any]]:
    """把模型写的名字对到高德候选上：相等 → 包含 → 字符相似度

    包含匹配要求短串至少 3 个字且长度占比 ≥ 50%，
    否则 "西湖" 会错误匹配到 "西湖博物馆" 这类同名片区 POI。
    """
    target = _normalize_name(name)
    if not target:
        return None

    for item in candidates:
        if _normalize_name(item.get("name", "")) == target:
            return item

    contained: List[Dict[str, Any]] = []
    for item in candidates:
        candidate = _normalize_name(item.get("name", ""))
        if not candidate:
            continue
        shorter, longer = sorted((target, candidate), key=len)
        if len(shorter) >= 3 and shorter in longer and len(shorter) / len(longer) >= 0.5:
            contained.append(item)
    if contained:
        return max(contained, key=lambda i: len(_normalize_name(i.get("name", ""))))

    scored = [(_similarity(target, _normalize_name(i.get("name", ""))), i) for i in candidates]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if scored and scored[0][0] >= min_similarity:
        return scored[0][1]
    return None


# ==================== 编排器 ====================


class MultiAgentTripPlanner:
    """四步流水线：景点 → 天气 → 酒店 → 行程规划"""

    def __init__(self, *, llm=None, tool_executor=None) -> None:
        self.llm = llm if llm is not None else get_llm()
        self.amap = get_amap_service() if tool_executor is None else None
        self._execute_tool = tool_executor if tool_executor is not None else execute_tool
        self._on_step: Optional[Callable[[Dict[str, Any]], None]] = None
        self._should_cancel: Optional[Callable[[], bool]] = None
        self._token_usage = 0

    # ---------------------------------------------------------------- 回调
    def _emit(self, entry: Dict[str, Any]) -> None:
        """把一步的执行记录交给上层（runner 会落库并推 SSE），同时打一条结构化日志"""
        with bind_step(str(entry.get("step", "-"))):
            log.info(
                "pipeline.step",
                ok=bool(entry.get("ok", True)),
                tool=entry.get("tool"),
                items=entry.get("item_count"),
                latency_ms=entry.get("latency_ms"),
                mode=entry.get("mode"),
            )
        if self._on_step is not None:
            self._on_step(entry)

    def _check_cancel(self) -> None:
        """检查点：用户取消就立刻退出，不等当前步骤跑完"""
        if self._should_cancel is not None and self._should_cancel():
            raise TaskCanceled("任务已被用户取消")

    # ---------------------------------------------------------------- 工具步
    def _tool_step(
        self,
        step: str,
        tool_name: str,
        system_prompt: str,
        user_prompt: str,
        result: PlanResult,
        count_key: str,
    ) -> Optional[Dict[str, Any]]:
        started = time.perf_counter()
        self._check_cancel()
        try:
            arguments, meta = self.llm.call_with_tool(
                system_prompt, user_prompt, get_tool_schema(tool_name)
            )
            data = self._execute_tool(tool_name, arguments)
        except (LLMError, AmapError, KeyError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            entry = {
                "step": step,
                "ok": False,
                "detail": str(exc),
                "tool": tool_name,
                "latency_ms": elapsed,
            }
            result.trace.append(entry)
            self._emit(entry)
            result.warnings.append(f"{step}失败：{exc}")
            return None

        count = int(data.get(count_key, 0)) if isinstance(data, dict) else 0
        elapsed = int((time.perf_counter() - started) * 1000)
        self._token_usage += int((meta.get("usage") or {}).get("total_tokens", 0))
        entry = {
            "step": step,
            "ok": True,
            "tool": tool_name,
            "arguments": arguments,
            "item_count": count,
            "latency_ms": elapsed,
            "mode": meta.get("mode"),
            "detail": f"{tool_name} 返回 {count} 条",
        }
        result.trace.append(entry)
        self._emit(entry)
        return data

    # ---------------------------------------------------------------- 步骤 1
    def _step_attractions(
        self, request: TripRequest, result: PlanResult
    ) -> List[Dict[str, Any]]:
        preference = request.preferences[0] if request.preferences else ""
        keyword = preference or "景点"

        queries: List[str] = [keyword]
        if request.preferences[1:]:
            queries.append(request.preferences[1])
        if keyword != "景点":
            queries.append("景点")

        merged: Dict[str, Dict[str, Any]] = {}
        for query in queries[:3]:
            prompt = f"城市：{request.city}\n用户偏好：{query}"
            data = self._tool_step(
                step=f"景点搜索({query})",
                tool_name="amap_search_poi",
                system_prompt=POI_PARAM_PROMPT,
                user_prompt=prompt,
                result=result,
                count_key="count",
            )
            for poi in (data or {}).get("pois", []):
                merged.setdefault(poi["id"] or poi["name"], poi)
            if len(merged) >= 8:
                break

        pois = list(merged.values())
        result.sources["attractions"] = len(pois)
        if not pois:
            result.warnings.append(
                "未能从高德取到景点数据，行程中的景点与坐标均由模型生成，未经校验"
            )
        return pois

    # ---------------------------------------------------------------- 步骤 2
    def _step_weather(self, request: TripRequest, result: PlanResult) -> List[Dict[str, Any]]:
        data = self._tool_step(
            step="天气查询",
            tool_name="amap_get_weather",
            system_prompt=WEATHER_PARAM_PROMPT,
            user_prompt=f"城市：{request.city}",
            result=result,
            count_key="count",
        )
        forecasts = (data or {}).get("forecasts", [])
        if data and not forecasts:
            result.warnings.append("高德天气接口未返回预报数据")
        elif not data:
            pass  # 已在 _tool_step 里写入 warning

        result.sources["weather"] = len(forecasts)
        if forecasts:
            covered = {f.get("date") for f in forecasts}
            try:
                start = datetime.strptime(request.start_date, "%Y-%m-%d").date()
                end = datetime.strptime(request.end_date, "%Y-%m-%d").date()
                missing_days = [
                    (start + timedelta(days=i)).isoformat()
                    for i in range((end - start).days + 1)
                    if (start + timedelta(days=i)).isoformat() not in covered
                ]
            except ValueError:
                missing_days = []
            if missing_days:
                result.warnings.append(
                    f"高德预报仅覆盖未来 {len(forecasts)} 天，"
                    f"{missing_days[0]} 起共 {len(missing_days)} 天无预报，天气字段留空"
                )
        return forecasts

    # ---------------------------------------------------------------- 步骤 3
    def _step_hotels(
        self, request: TripRequest, pois: List[Dict[str, Any]], result: PlanResult
    ) -> List[Dict[str, Any]]:
        near = request.hotel_location_detail or request.hotel_location or ""
        prompt = (
            f"城市：{request.city}\n"
            f"住宿档次：{request.accommodation}\n"
            f"酒店品牌：{request.hotel_brand or '不限'}\n"
            f"位置偏好：{near or '不限'}"
        )
        data = self._tool_step(
            step="酒店搜索",
            tool_name="amap_search_hotel",
            system_prompt=HOTEL_PARAM_PROMPT,
            user_prompt=prompt,
            result=result,
            count_key="count",
        )
        hotels = (data or {}).get("hotels", [])

        # 按到景点群中心的距离排序，把"离景点近"变成可计算的事实交模型参考
        if hotels and pois:
            center = {
                "longitude": sum(p["location"]["longitude"] for p in pois) / len(pois),
                "latitude": sum(p["location"]["latitude"] for p in pois) / len(pois),
            }
            for hotel in hotels:
                hotel["distance_km"] = round(_distance_km(hotel["location"], center), 1)
            hotels.sort(key=lambda h: h["distance_km"])
            if hotels[0]["distance_km"] > 15:
                result.warnings.append(
                    f"附近 8 公里内没有搜到合适酒店，最近候选距景点群 {hotels[0]['distance_km']} 公里"
                )

        result.sources["hotels"] = len(hotels)
        if not hotels:
            result.warnings.append("未能从高德取到酒店数据，酒店信息由模型生成")
        return hotels

    # ---------------------------------------------------------------- 步骤 4
    def _step_plan(
        self,
        request: TripRequest,
        pois: List[Dict[str, Any]],
        forecasts: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
        result: PlanResult,
    ) -> TripPlan:
        payload = {
            "基本要求": {
                "城市": request.city,
                "开始日期": request.start_date,
                "结束日期": request.end_date,
                "天数": request.travel_days,
                "交通方式": request.transportation,
                "住宿偏好": request.accommodation,
                "旅行偏好": request.preferences,
                "额外要求": request.free_text_input or "无",
            },
            "候选景点（高德真实数据）": pois[:12],
            "真实天气预报（高德）": forecasts,
            "候选酒店（高德真实数据）": hotels[:8],
        }

        started = time.perf_counter()
        self._check_cancel()
        try:
            data, meta = self.llm.call_json(
                PLANNER_PROMPT,
                f"请据此生成行程，只输出 JSON：\n{_json_dumps(payload)}",
                max_tokens=8000,
            )
        except LLMError as exc:
            entry = {
                "step": "行程规划",
                "ok": False,
                "detail": str(exc),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
            result.trace.append(entry)
            self._emit(entry)
            raise PlanGenerationError(f"行程规划失败：{exc}") from exc

        self._token_usage += int((meta.get("usage") or {}).get("total_tokens", 0))
        entry = {
            "step": "行程规划",
            "ok": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "mode": meta.get("mode"),
            "detail": "模型输出 JSON 并完成校验",
        }
        result.trace.append(entry)
        self._emit(entry)

        plan = self._validate(data, request, result)
        self._ground(plan, request, pois, hotels, forecasts, result)
        return plan

    # ---------------------------------------------------------------- 校验
    def _validate(self, data: Dict[str, Any], request: TripRequest, result: PlanResult) -> TripPlan:
        data.setdefault("city", request.city)
        data["city"] = request.city
        data["start_date"] = request.start_date
        data["end_date"] = request.end_date
        data.setdefault("days", [])
        data.setdefault("overall_suggestions", "")

        if not isinstance(data.get("days"), list) or not data["days"]:
            raise PlanGenerationError("模型没有返回任何日程（days 为空）")

        for index, day in enumerate(data["days"]):
            if not isinstance(day, dict):
                raise PlanGenerationError("日程结构异常")
            day["day_index"] = index
            day.setdefault("date", _shift_date(request.start_date, index))
            day.setdefault("attractions", [])
            day.setdefault("meals", [])

        try:
            plan = TripPlan(**data)
        except Exception as exc:  # pydantic.ValidationError
            raise PlanGenerationError(f"行程结构校验失败：{exc}") from exc

        if len(plan.days) != request.travel_days:
            result.warnings.append(
                f"模型返回 {len(plan.days)} 天日程，与请求的 {request.travel_days} 天不一致"
            )
        return plan

    # ---------------------------------------------------------------- 回填
    def _ground(
        self,
        plan: TripPlan,
        request: TripRequest,
        pois: List[Dict[str, Any]],
        hotels: List[Dict[str, Any]],
        forecasts: List[Dict[str, Any]],
        result: PlanResult,
    ) -> None:
        matched_pois = 0
        matched_hotels = 0
        replaced_hotels = 0
        dropped = 0

        for day in plan.days:
            kept: List[Attraction] = []
            for attraction in day.attractions:
                poi = _best_match(attraction.name, pois)
                if poi:
                    attraction.name = poi["name"]
                    attraction.address = poi.get("address") or attraction.address
                    attraction.location = Location(**poi["location"])
                    attraction.poi_id = poi.get("id", "")
                    attraction.rating = poi.get("rating") or attraction.rating
                    attraction.category = attraction.category or poi.get("type", "")
                    attraction.source = "amap"
                    matched_pois += 1
                else:
                    attraction.source = "llm"

                if attraction.location is None:
                    # 没有坐标的景点无法上图，且大概率是模型虚构的，直接丢弃并记录
                    dropped += 1
                    continue
                kept.append(attraction)

            day.attractions = kept

            if day.hotel:
                original_name = day.hotel.name
                # 酒店比景点更容易撞名（同一连锁不同分店），阈值收得更严
                hotel = _best_match(day.hotel.name, hotels, min_similarity=0.75)
                if hotel is None and hotels:
                    # 模型推荐的酒店不在高德候选里 → 用离景点群最近的真实酒店替换，
                    # 而不是保留一份无法核实的地址和坐标
                    hotel = min(hotels, key=lambda h: h.get("distance_km", 999))
                    replaced_hotels += 1

                if hotel is not None:
                    day.hotel.name = hotel["name"]
                    day.hotel.address = hotel.get("address") or day.hotel.address
                    if hotel.get("location"):
                        day.hotel.location = Location(**hotel["location"])
                    if hotel.get("rating") is not None:
                        day.hotel.rating = str(hotel["rating"])
                    if not day.hotel.estimated_cost and hotel.get("cost"):
                        day.hotel.estimated_cost = int(hotel["cost"])
                    if hotel.get("distance_km") is not None:
                        day.hotel.distance = f"距景点群约 {hotel['distance_km']} 公里"
                    day.hotel.source = "amap"
                    matched_hotels += 1
                    # 描述里如果提到了别的分店名，同步改掉，避免正文和酒店字段打架
                    _rewrite_hotel_mentions(day, hotels, day.hotel.name, original_name)
                else:
                    day.hotel.source = "llm"

        if dropped:
            result.warnings.append(f"有 {dropped} 个景点无法匹配高德坐标，已从行程中移除")
        if pois and matched_pois == 0:
            result.warnings.append("景点名称与高德候选全部未匹配上，坐标未经校验")
        if hotels and matched_hotels == 0:
            result.warnings.append("酒店名称未匹配到高德数据，地址未经校验")
        if replaced_hotels:
            result.warnings.append(
                f"{replaced_hotels} 天的酒店未匹配到高德候选，已替换为距景点群最近的真实酒店"
            )

        result.sources["attractions_grounded"] = matched_pois
        result.sources["hotels_grounded"] = matched_hotels

        # 天气：只保留高德真实预报，按日期对齐，绝不采用模型编的温度
        real_weather = {f.get("date"): f for f in forecasts if f.get("date")}
        if real_weather:
            plan.weather_info = [
                WeatherInfo(**real_weather[day.date])
                for day in plan.days
                if day.date in real_weather
            ]
        else:
            plan.weather_info = []

        # 预算兜底
        if plan.budget is None:
            tickets = sum(a.ticket_price for d in plan.days for a in d.attractions)
            meals = sum(m.estimated_cost for d in plan.days for m in d.meals)
            hotel_costs = [
                d.hotel.estimated_cost for d in plan.days if d.hotel and d.hotel.estimated_cost
            ]
            rooms = sum(hotel_costs) if hotel_costs else 0
            plan.budget = Budget(
                total_attractions=tickets,
                total_hotels=rooms,
                total_meals=meals,
                total_transportation=0,
                total=tickets + meals + rooms,
            )
            result.warnings.append("模型未给出预算，已按门票+餐饮+住宿自动汇总")

    # ---------------------------------------------------------------- 主流程
    def plan_trip(
        self,
        request: TripRequest,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> PlanResult:
        """跑完整条流水线

        on_step       每完成一步回调一次（runner 用它落库 + 推 SSE）
        should_cancel 每个检查点调用一次；返回 True 抛 TaskCanceled
        """
        started = time.perf_counter()
        result = PlanResult(plan=None)  # type: ignore[arg-type]
        self._on_step = on_step
        self._should_cancel = should_cancel
        self._token_usage = 0

        pois = self._step_attractions(request, result)
        forecasts = self._step_weather(request, result)
        hotels = self._step_hotels(request, pois, result)
        result.plan = self._step_plan(request, pois, forecasts, hotels, result)

        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        result.token_usage = self._token_usage
        entry = {
            "step": "完成",
            "ok": True,
            "latency_ms": result.elapsed_ms,
            "detail": f"总计 {len(result.warnings)} 条降级提示",
        }
        result.trace.append(entry)
        self._emit(entry)
        return result

    # ---------------------------------------------------------------- 自检
    def health(self) -> Dict[str, Any]:
        """给 /api/trip/health 用：报告真实可用的工具与模型配置"""
        return {
            "llm_model": self.llm.model,
            "llm_base_url": self.llm.base_url,
            "tools": list_tools(),
            "amap_key_configured": bool(self.amap and self.amap.key),
            "pipeline": ["景点搜索", "天气查询", "酒店搜索", "行程规划"],
            "forced_tool_choice": self.llm._forced_tool,  # noqa: SLF001 - 自检需要
        }


# ==================== 工具函数 ====================


def _json_dumps(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=1)


def _rewrite_hotel_mentions(
    day: DayPlan, candidates: List[Dict[str, Any]], chosen_name: str, original_name: str
) -> None:
    """把正文里提到的其它分店名/旧名统一成最终选定的酒店名"""
    replacements = {original_name} if original_name and original_name != chosen_name else set()
    for item in candidates:
        name = item.get("name", "")
        if name and name != chosen_name:
            replacements.add(name)

    for attr in ("description", "accommodation"):
        text = getattr(day, attr) or ""
        for name in replacements:
            if name and name in text:
                text = text.replace(name, chosen_name)
        setattr(day, attr, text)


def _shift_date(start: str, days: int) -> str:
    try:
        base = datetime.strptime(start, "%Y-%m-%d")
    except ValueError:
        return start
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def get_trip_planner_agent() -> MultiAgentTripPlanner:
    """每次请求新建编排器：内部不持有对话历史，天然并发安全"""
    return MultiAgentTripPlanner()
