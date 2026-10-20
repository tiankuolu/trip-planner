"""数据模型定义

所有解析型字段（游览时长、门票、评分）都做了容错，因为模型输出的
"90分钟"、"免费"、"4.5分" 这类写法必须能落到强类型模型上，而不是
让整份行程因为一个字段校验失败而退化。
"""

import re
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


def _to_minutes(value: Any) -> int:
    """把 "90" / "90分钟" / "1.5小时" 统一成分钟"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return 120
    number = float(match.group(1))
    if "小时" in text or "hour" in text.lower():
        number *= 60
    return int(number)


def _to_yuan(value: Any) -> int:
    """把 "免费" / "¥60" / "60元" 统一成整数元"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text or "免费" in text or text in {"-", "无"}:
        return 0
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return int(float(match.group(1))) if match else 0


def _to_rating(value: Any) -> Optional[float]:
    if value in (None, "", "暂无", "无", "-"):
        return None
    try:
        return float(str(value).replace("分", "").strip())
    except (TypeError, ValueError):
        return None


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    city: str = Field(..., description="目的地城市", example="北京")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD", example="2025-06-01")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD", example="2025-06-03")
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30, example=3)
    transportation: str = Field(..., description="交通方式", example="公共交通")
    accommodation: str = Field(..., description="住宿偏好", example="经济型酒店")
    hotel_brand: str = Field(default="", description="酒店品牌: 如家系/华住系/锦江系", example="华住系")
    hotel_location: str = Field(default="", description="住宿位置偏好: 景点附近/大学附近/市中心/地铁沿线/火车站附近/机场附近", example="景点附近")
    hotel_location_detail: str = Field(default="", description="具体位置: 景点名/大学名/地铁站名", example="故宫")
    preferences: List[str] = Field(default=[], description="旅行偏好标签", example=["历史文化", "美食"])
    free_text_input: Optional[str] = Field(default="", description="额外要求", example="希望多安排一些博物馆")
    
    class Config:
        json_schema_extra = {
            "example": {
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆"
            }
        }


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", example="故宫")
    city: str = Field(..., description="城市", example="北京")
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", example="北京市朝阳区阜通东大街6号")
    destination_address: str = Field(..., description="终点地址", example="北京市海淀区上地十街10号")
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


# ============ 响应模型 ============

class Location(BaseModel):
    """地理位置"""
    longitude: float = Field(..., description="经度")
    latitude: float = Field(..., description="纬度")


class Attraction(BaseModel):
    """景点信息"""
    name: str = Field(..., description="景点名称")
    address: str = Field(default="", description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标（来自高德）")
    visit_duration: int = Field(default=120, description="建议游览时间(分钟)")
    description: str = Field(default="", description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="高德 POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, description="门票价格(元)")
    source: Optional[str] = Field(
        default="llm", description="坐标来源：amap=高德回填, llm=模型给出(未校验)"
    )

    @field_validator("visit_duration", mode="before")
    @classmethod
    def _duration(cls, v: Any) -> int:
        return _to_minutes(v)

    @field_validator("ticket_price", mode="before")
    @classmethod
    def _ticket(cls, v: Any) -> int:
        return _to_yuan(v)

    @field_validator("rating", mode="before")
    @classmethod
    def _rating(cls, v: Any) -> Optional[float]:
        return _to_rating(v)


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, description="预估费用(元)")

    @field_validator("estimated_cost", mode="before")
    @classmethod
    def _cost(cls, v: Any) -> int:
        return _to_yuan(v)


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, description="预估费用(元/晚)")
    source: Optional[str] = Field(default="llm", description="来源：amap=高德回填")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(default=0, description="第几天(从0开始)")
    description: str = Field(default="", description="当日行程描述")
    transportation: str = Field(default="", description="交通方式")
    accommodation: str = Field(default="", description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default=[], description="景点列表")
    meals: List[Meal] = Field(default=[], description="餐饮列表")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")


class TraceStep(BaseModel):
    """一次工具调用/模型调用的执行记录（用于可观测性）"""
    step: str = Field(..., description="步骤名")
    ok: bool = Field(default=True, description="是否成功")
    detail: str = Field(default="", description="说明")
    tool: Optional[str] = Field(default=None, description="调用的工具名")
    arguments: Optional[dict] = Field(default=None, description="工具入参")
    item_count: Optional[int] = Field(default=None, description="工具返回条数")
    latency_ms: int = Field(default=0, description="耗时(毫秒)")
    mode: Optional[str] = Field(default=None, description="function_call / text_fallback / json_object")


# ============ 任务化接口 ============

class TaskSubmitResponse(BaseModel):
    """提交任务后的 202 响应"""
    task_id: str = Field(..., description="任务 ID")
    status: str = Field(..., description="当前状态，提交后为 pending")
    status_url: str = Field(..., description="查询状态的地址")
    events_url: str = Field(..., description="SSE 事件流地址")
    deduplicated: bool = Field(default=False, description="是否命中幂等键（复用了已有任务）")


class TaskStepView(BaseModel):
    """任务步骤视图（落库的 trace 一条）"""
    step: str
    status: str
    tool: Optional[str] = None
    arguments: Optional[dict] = None
    item_count: Optional[int] = None
    latency_ms: int = 0
    mode: Optional[str] = None
    detail: Optional[str] = None


class TaskDetailResponse(BaseModel):
    """任务详情"""
    task_id: str
    status: str = Field(..., description="pending / running / succeeded / failed / canceled")
    city: str
    start_date: str
    end_date: str
    travel_days: int
    degraded: bool = False
    warnings: List[str] = Field(default_factory=list)
    data_sources: dict = Field(default_factory=dict)
    error: Optional[dict] = Field(default=None, description="失败原因 {code, message}")
    attempt_count: int = 0
    elapsed_ms: int = 0
    token_usage: int = 0
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    steps: List[TaskStepView] = Field(default_factory=list)
    plan: Optional["TripPlan"] = Field(default=None, description="成功后才有")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default=[], description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")


class TripPlanResponse(BaseModel):
    """旅行计划响应

    degraded=True 表示有步骤失败或数据未取到（例如天气接口不可用），
    warnings 会逐条说明原因，前端据此展示黄条提示，而不是假装一切正常。
    """
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")
    degraded: bool = Field(default=False, description="是否有步骤降级")
    warnings: List[str] = Field(default_factory=list, description="降级原因说明")
    trace: List[TraceStep] = Field(default_factory=list, description="执行轨迹")
    elapsed_ms: int = Field(default=0, description="总耗时(毫秒)")
    data_sources: dict = Field(
        default_factory=dict, description="各步骤数据来源统计"
    )


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default=[], description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default=[], description="天气信息")


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")

